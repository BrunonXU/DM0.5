#!/usr/bin/env python3
"""Freeze and deploy a bounded F1 source snapshot. Never runs robot/task code.

Only a new deployment root may be activated automatically. Subsequent releases
require a separate, verified backup workflow; this tool refuses to overwrite an
existing release or field changes. Repeating the same release only verifies it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import inspect
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile

TASK = "F1/foam_sort_whole_body"
DEPLOY_BASE = "/home/user/Bruno"
TOOL_ID = "f1-static-source-snapshot-v1"
KIND = "source_snapshot_not_runtime_verified"
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
MAX_ARCHIVE = 25 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml",
                 ".txt", ".xml", ".ini", ".cfg", ".cpp", ".hpp", ".h",
                 ".c", ".msg", ".srv", ".action", ".urdf", ".xacro", ".service"}
SKIP_DIRS = {".git", ".ssh", ".venv", "venv", "env", "__pycache__", ".cache",
             "node_modules", "weights", "checkpoints", "raw", "videos", "previews",
             "aligned", "signals", "cameras", "deployments", "build", "log"}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid relative path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or any(p in {"..", "."} for p in path.parts) or path.as_posix() != value:
        raise ValueError("Unsafe relative path: " + value)
    return path


def validate_release_id(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("Release ID must be 1–80 letters, digits, underscores, dots or hyphens")
    return value


def validate_target_root(value):
    """Check the remote path lexically; filesystem checks run on F1 itself."""
    expected = (Path(DEPLOY_BASE) / TASK).as_posix()
    if os.fspath(value) != expected:
        raise ValueError("F1 deployment target must be exactly " + expected)
    return Path(expected)


def ensure_no_symlink(path):
    path = Path(path)
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise ValueError("Symlink is not allowed here: " + str(component))


def validate_manifest(manifest):
    if manifest.get("tool_id") != TOOL_ID or manifest.get("deployment_kind") != KIND:
        raise ValueError("Unsupported manifest")
    validate_release_id(manifest["release_id"])
    if manifest.get("task") != TASK or manifest.get("training_started") is not False or manifest.get("robot_control_started") is not False:
        raise ValueError("Manifest must describe a non-running source snapshot")
    entries = manifest["files"]
    if not entries or len(entries) > 10000:
        raise ValueError("Invalid file count")
    found = set()
    for entry in entries:
        name = str(safe_relative(entry["path"]))
        if name in found or not (name == "PROJECT_RULES_AND_PREFERENCES.md" or name.startswith("F1/")):
            raise ValueError("Duplicate or out-of-scope manifest path: " + name)
        found.add(name)
        if type(entry["size_bytes"]) is not int or not 0 <= entry["size_bytes"] <= MAX_FILE:
            raise ValueError("Invalid file size: " + name)
        if entry["mode"] not in (0o644, 0o755) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ValueError("Invalid file metadata: " + name)
    if sum(e["size_bytes"] for e in entries) > MAX_TOTAL:
        raise ValueError("Snapshot exceeds total size limit")


def validate_archive(archive, expected_manifest=None):
    """Read bounded regular files; never call extract/extractall."""
    payload = {}
    total = 0
    seen = set()
    options = {"fileobj": archive} if hasattr(archive, "read") else {"name": archive}
    with tarfile.open(mode="r:gz", **options) as tar:
        for member in tar:
            name = str(safe_relative(member.name))
            if name in seen:
                raise ValueError("Duplicate tar member: " + name)
            seen.add(name)
            if member.isdir():
                if name != "workspace" and not name.startswith("workspace/"):
                    raise ValueError("Unexpected archive directory")
                continue
            if not member.isfile() or member.size > MAX_FILE or member.size < 0:
                raise ValueError("Archive links, special files or oversized files are forbidden")
            if name != "manifest.json" and not name.startswith("workspace/"):
                raise ValueError("Unexpected archive member: " + name)
            total += member.size
            if total > MAX_TOTAL + MAX_FILE or len(seen) > 20000:
                raise ValueError("Archive exceeds bounds")
            handle = tar.extractfile(member)
            data = handle.read(MAX_FILE + 1)
            if len(data) != member.size:
                raise ValueError("Archive member size mismatch")
            payload[name] = data
    manifest_data = payload.pop("manifest.json", None)
    if manifest_data is None:
        raise ValueError("Archive manifest missing")
    manifest = json.loads(manifest_data)
    validate_manifest(manifest)
    if expected_manifest is not None and manifest_data != expected_manifest:
        raise ValueError("External and archive manifests differ")
    expected = {"workspace/" + e["path"] for e in manifest["files"]}
    if set(payload) != expected:
        raise ValueError("Archive file set differs from manifest")
    for entry in manifest["files"]:
        data = payload["workspace/" + entry["path"]]
        if len(data) != entry["size_bytes"] or digest(data) != entry["sha256"]:
            raise ValueError("Archive file checksum mismatch: " + entry["path"])
    return manifest, manifest_data, payload


def verify_tree(release_dir, manifest_data=None):
    release_dir = Path(release_dir)
    ensure_no_symlink(release_dir)
    if manifest_data is None:
        manifest_path = release_dir / "manifest.json"
        ensure_no_symlink(manifest_path)
        manifest_data = manifest_path.read_bytes()
    manifest = json.loads(manifest_data)
    validate_manifest(manifest)
    workspace = release_dir / "workspace"
    ensure_no_symlink(workspace)
    expected = {e["path"] for e in manifest["files"]}
    actual = set()
    for parent, dirs, files in os.walk(workspace, followlinks=False):
        for name in dirs + files:
            ensure_no_symlink(Path(parent) / name)
        for name in files:
            actual.add((Path(parent) / name).relative_to(workspace).as_posix())
    if actual != expected:
        raise ValueError("Release contains missing or unrecorded files")
    for entry in manifest["files"]:
        path = workspace / entry["path"]
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size_bytes"]:
            raise ValueError("Remote file type/size mismatch: " + entry["path"])
        if stat.S_IMODE(info.st_mode) != entry["mode"] or digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError("Remote file mode/hash mismatch: " + entry["path"])
    return {"release_id": manifest["release_id"], "file_count": len(expected),
            "total_bytes": sum(e["size_bytes"] for e in manifest["files"]),
            "manifest_sha256": digest(manifest_data), "files_verified": True,
            "deployment_kind": KIND, "dependency_check": "not_run",
            "onsite_replay": "not_run", "training_started": False,
            "robot_control_started": False}


def selected_files(workspace):
    workspace = Path(workspace).absolute()
    ensure_no_symlink(workspace)
    roots = [workspace / TASK, workspace / "F1/reference", workspace / "F1/docs"]
    fixed = ["PROJECT_RULES_AND_PREFERENCES.md", "F1/AGENTS.md", "F1/README.md",
             "F1/F1系统配置.md", "F1/F1系统接口清单.md", "F1/通用机器人软件接口文档.md"]
    selected = {workspace / p for p in fixed}
    skipped = []
    for root in roots:
        ensure_no_symlink(root)
        if not root.is_dir():
            raise ValueError("Required source directory missing: " + str(root))
        for parent, dirs, files in os.walk(root, followlinks=False):
            kept_dirs = []
            for name in dirs:
                if name in SKIP_DIRS or name.startswith("."):
                    continue
                path = Path(parent) / name
                ensure_no_symlink(path)
                kept_dirs.append(name)
            dirs[:] = sorted(kept_dirs)
            for name in sorted(files):
                path = Path(parent) / name
                relative = path.relative_to(workspace)
                if name.startswith(".") or path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if "results" in relative.parts and path.suffix != ".md" and name not in {"summary.json", "validation.json"}:
                    continue
                ensure_no_symlink(path)
                if path.stat().st_size > MAX_FILE:
                    skipped.append({"path": relative.as_posix(), "reason": "over_2_MiB"})
                    continue
                selected.add(path)
    frozen = {}
    for path in sorted(selected):
        ensure_no_symlink(path)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
            raise ValueError("Required source is not a bounded regular file: " + str(path))
        data = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
            raise ValueError("Source changed while freezing: " + str(path))
        data.decode("utf-8")
        if b"\x00" in data or re.search(br"-----BEGIN [A-Z ]*PRIVATE KEY-----", data):
            raise ValueError("Binary content or private key in source: " + str(path))
        mode = 0o755 if before.st_mode & 0o111 else 0o644
        frozen[path.relative_to(workspace).as_posix()] = (data, mode)
    if sum(len(data) for data, _ in frozen.values()) > MAX_TOTAL:
        raise ValueError("Selected source exceeds 20 MiB")
    return frozen, skipped


def prepare(workspace, release_id, output=None):
    release_id = validate_release_id(release_id)
    workspace = Path(workspace).absolute()
    output = Path(output) if output else workspace / TASK / "results/deployments" / release_id
    ensure_no_symlink(output)
    if output.exists():
        raise ValueError("Release output already exists; choose a new ID")
    frozen, skipped = selected_files(workspace)
    git_result = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain", "--untracked-files=normal", "--", "F1", "PROJECT_RULES_AND_PREFERENCES.md"], capture_output=True, text=True)
    manifest = {"schema_version": 1, "tool_id": TOOL_ID, "deployment_kind": KIND,
                "release_id": release_id, "task": TASK, "created_at": utc_now(),
                "source": {"host": platform.node(), "workspace": str(workspace),
                           "git_commit": git_result.stdout.strip() if git_result.returncode == 0 else None,
                           "working_tree_dirty": bool(dirty.stdout) if dirty.returncode == 0 else None,
                           "content_authority": "actual_selected_file_bytes_and_sha256"},
                "training_started": False, "robot_control_started": False,
                "files": [{"path": path, "size_bytes": len(data), "sha256": digest(data), "mode": mode}
                          for path, (data, mode) in frozen.items()],
                "excluded_large_text_files": skipped,
                "excluded_scope": ["datasets", "weights", "binary_artifacts", "virtual_environments", "large_results", "shared_DM0.5_runtime"]}
    validate_manifest(manifest)
    manifest_data = json_bytes(manifest)
    output.mkdir(parents=True)
    (output / "manifest.json").write_bytes(manifest_data)
    archive = output / "package.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, data, mode in [("manifest.json", manifest_data, 0o644)] + [("workspace/" + p, d, m) for p, (d, m) in frozen.items()]:
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), mode, 0
            tar.addfile(member, io.BytesIO(data))
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Compressed package exceeds bounds")
    (output / "package.sha256").write_text(digest(archive.read_bytes()) + "\n")
    validate_archive(archive, manifest_data)
    return {"release_id": release_id, "directory": str(output), "files": len(frozen),
            "source_bytes": sum(len(d) for d, _ in frozen.values()), "package_bytes": archive.stat().st_size,
            "manifest_sha256": digest(manifest_data), "package_sha256": digest(archive.read_bytes()),
            "deployment_kind": KIND}


def load_package(directory, compare_source=False):
    directory = Path(directory).absolute()
    for name in ("manifest.json", "package.tar.gz", "package.sha256"):
        ensure_no_symlink(directory / name)
    archive = directory / "package.tar.gz"
    if archive.stat().st_size > MAX_ARCHIVE or digest(archive.read_bytes()) != (directory / "package.sha256").read_text().strip():
        raise ValueError("Local archive checksum or size mismatch")
    manifest_data = (directory / "manifest.json").read_bytes()
    manifest, _, _ = validate_archive(archive, manifest_data)
    if compare_source:
        frozen, _ = selected_files(manifest["source"]["workspace"])
        actual = {p: (digest(d), len(d), m) for p, (d, m) in frozen.items()}
        expected = {e["path"]: (e["sha256"], e["size_bytes"], e["mode"]) for e in manifest["files"]}
        changed = sorted(p for p in actual.keys() | expected.keys() if actual.get(p) != expected.get(p))
        if changed:
            raise ValueError("Source differs from frozen release: " + ", ".join(changed[:20]))
    return manifest, manifest_data


def remote_state(root):
    root = Path(root)
    ensure_no_symlink(root)
    if not root.exists():
        return {"target_exists": False, "current_target": None, "managed": False}
    if not root.is_dir():
        raise ValueError("Target is not a directory")
    marker = root / "deployment_state.json"
    ensure_no_symlink(marker)
    current = root / "current"
    if not marker.is_file() or not current.is_symlink():
        raise ValueError("Existing target is not a completed managed deployment; refusing modification")
    state = json.loads(marker.read_text())
    if state.get("tool_id") != TOOL_ID:
        raise ValueError("Target has foreign deployment metadata")
    release_id = validate_release_id(state["release_id"])
    expected = "releases/" + release_id + "/workspace/" + TASK
    if os.readlink(current) != expected:
        raise ValueError("Current link differs from recorded managed release")
    return {"target_exists": True, "current_target": expected, "managed": True, "release_id": release_id}


def remote_main():
    operation, root_arg, release_id, expected_host, expected_user, expected_arch, manifest_sha = sys.argv[1:]
    validate_release_id(release_id)
    identity = {"host": platform.node(), "user": getpass.getuser(), "architecture": platform.machine()}
    if identity != {"host": expected_host, "user": expected_user, "architecture": expected_arch}:
        raise ValueError("SSH endpoint identity differs from expected F1 host/user/architecture")
    root = validate_target_root(root_arg)
    before = remote_state(root)
    if operation == "preflight":
        print(json.dumps({**before, "identity": identity}))
        return
    release = root / "releases" / release_id
    if operation == "verify":
        manifest_path = release / "manifest.json"
        ensure_no_symlink(manifest_path)
        if digest(manifest_path.read_bytes()) != manifest_sha:
            raise ValueError("Remote manifest differs from local frozen release")
        print(json.dumps({**verify_tree(release), "current_target": before["current_target"],
                          "active": before.get("release_id") == release_id, "identity": identity}))
        return
    if operation != "deploy":
        raise ValueError("Unknown remote operation")
    if before["target_exists"]:
        if before["release_id"] != release_id:
            raise ValueError("Existing release requires verified local backup before a new activation; no files changed")
        if digest((release / "manifest.json").read_bytes()) != manifest_sha:
            raise ValueError("Release ID already exists with different content")
        print(json.dumps({**verify_tree(release), "idempotent_verify_only": True,
                          "current_target": before["current_target"], "active": True, "identity": identity}))
        return
    with tempfile.TemporaryFile() as archive:
        count = 0
        while True:
            chunk = sys.stdin.buffer.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_ARCHIVE:
                raise ValueError("Uploaded archive too large")
            archive.write(chunk)
        archive.seek(0)
        manifest, manifest_data, payload = validate_archive(archive)
    if manifest["release_id"] != release_id or digest(manifest_data) != manifest_sha:
        raise ValueError("Uploaded manifest identity differs from prepared version")
    # Check again after reading the archive; mkdir is exclusive. Never delete old files.
    ensure_no_symlink(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(exist_ok=False)
    release.mkdir(parents=True, exist_ok=False)
    for entry in manifest["files"]:
        destination = release / "workspace" / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(payload["workspace/" + entry["path"]])
        destination.chmod(entry["mode"])
    (release / "manifest.json").write_bytes(manifest_data)
    report = verify_tree(release)
    (root / "runtime").mkdir()
    current_target = "releases/" + release_id + "/workspace/" + TASK
    (root / "current").symlink_to(current_target)
    for name in ("robot", "models", "configs", "docs", "scripts", "deploy", "tests"):
        if (release / "workspace" / TASK / name).is_dir():
            (root / name).symlink_to("current/" + name)
    with (root / "README.md").open("x") as handle:
        handle.write("# F1 foam sorting deployment\n\nOpen `current/` as the task project.\n"
                     "Shared F1 references are preserved in the release workspace.\n"
                     "This is a verified source snapshot, not a verified runtime.\n"
                     "No training, ROS node, controller or robot action was started.\n"
                     "Runtime outputs belong in `runtime/`; keep releases as deployed.\n")
    state = {"tool_id": TOOL_ID, "release_id": release_id, "activated_at": utc_now(),
             "current_target": current_target, "deployment_kind": KIND,
             "previous_current_target": None, "previous_target_existed": False}
    with (root / "deployment_state.json").open("x") as handle:
        handle.write(json_bytes(state).decode())
    print(json.dumps({**report, **state, "active": True, "identity": identity,
                      "rollback_check": "not_applicable_first_deployment_no_previous_version"}))


def remote_program():
    constants = "\n".join(name + " = " + repr(globals()[name]) for name in
                          ("TASK", "DEPLOY_BASE", "TOOL_ID", "KIND", "MAX_FILE", "MAX_TOTAL", "MAX_ARCHIVE"))
    imports = "import datetime as dt, getpass, hashlib, json, os, platform, re, stat, sys, tarfile, tempfile\nfrom pathlib import Path, PurePosixPath\n"
    functions = (utc_now, digest, json_bytes, safe_relative, validate_release_id, validate_target_root,
                 ensure_no_symlink, validate_manifest, validate_archive, verify_tree,
                 remote_state, remote_main)
    return imports + constants + "\n\n" + "\n\n".join(inspect.getsource(f) for f in functions) + "\nremote_main()\n"


def ssh_command(args, operation, release_id, manifest_sha):
    root = validate_target_root(args.target_root)
    remote = ["python3", "-c", remote_program(), operation, str(root), release_id,
              "ms-thor", "user", "aarch64", manifest_sha]
    return ["ssh", "-F", "/dev/null", "-i", str(Path(args.key).expanduser()),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "HostKeyAlias=f1-d070", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-p", str(args.port), "user@127.0.0.1", shlex.join(remote)]


def call_remote(args, operation, release_id, manifest_sha, archive=None):
    command = ssh_command(args, operation, release_id, manifest_sha)
    if archive is None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    else:
        with Path(archive).open("rb") as handle:
            result = subprocess.run(command, stdin=handle, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError("Remote " + operation + " failed: " + result.stderr[-4000:])
    return json.loads(result.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("prepare", help="Freeze selected source; does not connect to F1")
    freeze.add_argument("--workspace", default=str(Path(__file__).absolute().parents[3]))
    freeze.add_argument("--release-id", required=True)
    freeze.add_argument("--output")
    for name in ("deploy", "verify", "check-package"):
        p = sub.add_parser(name)
        p.add_argument("--package-dir", required=True)
        p.add_argument("--compare-source", action="store_true")
        if name != "check-package":
            p.add_argument("--key", default="~/.ssh/id_ed25519_f1")
            p.add_argument("--port", type=int, default=22022)
            p.add_argument("--target-root", default=str(Path(DEPLOY_BASE) / TASK),
                           help="Must equal the F1 task directory under /home/user/Bruno")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.workspace, args.release_id, args.output)
    else:
        directory = Path(args.package_dir).absolute()
        manifest, manifest_data = load_package(directory, args.compare_source)
        release_id = manifest["release_id"]
        manifest_sha = digest(manifest_data)
        if args.command == "check-package":
            result = {"release_id": release_id, "files": len(manifest["files"]),
                      "package_verified": True, "source_compared": args.compare_source,
                      "manifest_sha256": manifest_sha}
        elif args.command == "verify":
            result = call_remote(args, "verify", release_id, manifest_sha)
            (directory / "remote_verification.json").write_bytes(json_bytes(result))
        else:
            before = call_remote(args, "preflight", release_id, manifest_sha)
            if before["target_exists"]:
                if before.get("release_id") != release_id:
                    raise ValueError("Existing F1 release requires actual local backup before replacement; deployment refused")
                result = call_remote(args, "verify", release_id, manifest_sha)
                result["idempotent_verify_only"] = True
            else:
                backup = directory / "backup"
                backup.mkdir(exist_ok=True)
                record = {"checked_at": utc_now(), "target_root": args.target_root,
                          "remote_state": before, "backup_status": "no_previous_deployment",
                          "files_backed_up": 0, "backup_performed": False}
                (backup / "predeployment_state.json").write_bytes(json_bytes(record))
                result = call_remote(args, "deploy", release_id, manifest_sha, directory / "package.tar.gz")
                checked = call_remote(args, "verify", release_id, manifest_sha)
                result["independent_remote_recheck"] = checked
            (directory / "deployment_report.json").write_bytes(json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
