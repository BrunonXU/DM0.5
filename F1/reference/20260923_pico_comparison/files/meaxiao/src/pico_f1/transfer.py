"""Committed-episode outbox and SSH pull merger. No ROS or motion controls.

Source commit boundary: recorder's .dataset.lock + meta/info.json.
Central commit boundary: an atomic symlink to an immutable dataset revision.
Only an authenticated SSH peer can read the outbox or acknowledge delivery.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import uuid
import threading

ROBOTS = ("F1_D070", "F1_D070_01", "F1_D071", "F1_D072")
DEFAULT_BASE = Path("/media/xx_100872445/069dd146-6f88-4f29-b859-245b1c012179/pico/mocap_data")
DEFAULT_DEST = DEFAULT_BASE / "mm/mocap_data"
REMOTE_PROJECT = "/home/user/meaxiao/pico"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()


def object_hash(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with tmp.open("xb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


@contextlib.contextmanager
def lock(path, shared=False, nonblock=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a") as stream:
        fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) |
                    (fcntl.LOCK_NB if nonblock else 0))
        yield


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


def safe_path(root, relative):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(p in ("..", ".") for p in rel.parts):
        raise ValueError("unsafe relative path")
    path = Path(root).joinpath(*rel.parts)
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("path escapes dataset")
    if any(c in relative for c in "\n\r\0"):
        raise ValueError("invalid path character")
    return path


def key_for(robot, dataset_uuid, episode):
    if robot not in ROBOTS:
        raise ValueError("unrecognized physical robot identity")
    uuid.UUID(dataset_uuid)
    return object_hash([robot, dataset_uuid, int(episode)])


def episode_files(root, row, info):
    from pico_f1.episode_analysis import episode_paths
    data, videos = episode_paths(root, row, info)
    metadata = root / "meta/episodes" / f"chunk-{int(row['meta/episodes/chunk_index']):03d}" / f"file-{int(row['meta/episodes/file_index']):03d}.parquet"
    paths = [data, *videos, metadata]
    alignment = root / "alignment" / f"episode-{int(row['episode_index']):06d}"
    if alignment.exists():
        paths += sorted(p for p in alignment.rglob("*") if p.is_file())
    return [str(p.relative_to(root)) for p in paths]


def enqueue(project, source, robot, destination):
    """Called by the singleton agent. Copy once; publish manifests only when complete."""
    import pyarrow.parquet as pq
    from pico_f1.lerobot_v3 import _read_episode_rows
    source = Path(source).resolve()
    home = Path(project) / "transfer_state"
    home.mkdir(parents=True, exist_ok=True)
    if not (source / "meta/info.json").exists():
        return []
    with lock(source / ".dataset.lock", shared=True):
        info = read(source / "meta/info.json")
        rows = [r for r in _read_episode_rows(source) if int(r["episode_index"]) < int(info["total_episodes"])]
        if [int(r["episode_index"]) for r in rows] != list(range(int(info["total_episodes"]))):
            raise ValueError("committed source episode index is incomplete")
        if sum(int(r["length"]) for r in rows) != int(info["total_frames"]):
            raise ValueError("source commit frame totals mismatch")
        tasks = pq.read_table(source / "meta/tasks.parquet").to_pylist()
    if not rows:
        return []
    # Generation marker lives WITH the dataset. A replaced episode 0 must not
    # inherit the identity of the previous collection, even if numbers restart.
    first = safe_path(source, episode_files(source, rows[0], info)[0])
    fingerprint = digest(first)
    identity_path = source / ".transfer_dataset_id.json"
    identity = read(identity_path) if identity_path.exists() else {}
    if identity.get("first_episode_sha256") != fingerprint:
        identity = dict(uuid=str(uuid.uuid4()), first_episode_sha256=fingerprint)
        atomic_json(identity_path, identity)
    queued = []
    for row in rows:
        episode = int(row["episode_index"])
        key = key_for(robot, identity["uuid"], episode)
        final = home / "outbox" / key
        if (final / "manifest.json").exists():
            continue  # Previously bound destination is immutable, including after ACK.
        if shutil.disk_usage(source).free < 5 * 1024**3:
            raise ValueError("F1可用空间不足5GiB，暂停建立传输副本；源数据未删除")
        stage = home / "preparing" / (key + "-" + uuid.uuid4().hex)
        payload = stage / "payload"
        payload.mkdir(parents=True)
        files = []
        try:
            for relative in episode_files(source, row, info):
                original = safe_path(source, relative)
                before = original.stat()
                target = safe_path(payload, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
                after = original.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError("source changed while freezing episode")
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
                files.append(dict(path=relative, size=target.stat().st_size, sha256=digest(target)))
            manifest = dict(protocol=1, key=key, robot_id=robot, dataset_uuid=identity["uuid"],
                            source_episode=episode, source_root=str(source), destination=str(destination),
                            row=row, info=info, tasks=tasks, files=files, queued_at=time.time())
            atomic_json(stage / "manifest.json", manifest)
            for directory in sorted((p for p in payload.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                fsync_dir(directory)
            fsync_dir(payload)
            fsync_dir(stage)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(stage, final)
            fsync_dir(final.parent)
            queued.append(key)
            log(f"[自动传输] 本地保存完整：{robot} episode={episode:06d}，已加入队列，等待6000d接收；目标={destination}")
        except Exception:
            shutil.rmtree(stage)
            raise
    return queued


def outbox_index(project):
    home = Path(project) / "transfer_state"
    result = []
    for path in sorted((home / "outbox").glob("*/manifest.json")):
        if not (home / "acks" / (path.parent.name + ".json")).exists():
            manifest = read(path)
            result.append(dict(key=manifest["key"], sha256=object_hash(manifest),
                               episode=manifest["source_episode"]))
    return sorted(result, key=lambda r: r["episode"])


def acknowledge(project, receipt):
    key = receipt["key"]
    if not re.fullmatch("[0-9a-f]{64}", key):
        raise ValueError("invalid key")
    home = Path(project) / "transfer_state"
    manifest = read(home / "outbox" / key / "manifest.json")
    if receipt["manifest_sha256"] != object_hash(manifest) or receipt["destination"] != manifest["destination"]:
        raise ValueError("receipt does not match frozen episode")
    atomic_json(home / "acks" / (key + ".json"), receipt)


def agent(project, source, robot, destination):
    home = project / "transfer_state"
    with lock(home / "agent.lock", nonblock=True):
        atomic_json(home / "agent.json", dict(pid=os.getpid(), destination=str(destination), source=str(source), robot=robot))
        log(f"[自动传输] 服务已启动：完整保存后自动排队，6000d通过SSH加密接收；目标={destination}")
        seen = set()
        session_marker = project / "run/collection_session.json"
        def heartbeat():
            from pico_f1.collection import control
            while session_marker.exists():
                try:
                    control(project, read(session_marker), "renew")
                except Exception as exc:
                    log(f"[数采会话] 中央心跳暂未确认，将重试：{exc}")
                time.sleep(15)
        if session_marker.exists():
            threading.Thread(target=heartbeat, name="collection-heartbeat", daemon=True).start()
        while True:
            try:
                if (home / "settings.json").exists():
                    destination = Path(read(home / "settings.json")["destination"])
                registry = home / "routes.json"
                if registry.exists():
                    for route in read(registry).values():
                        enqueue(project, Path(route["source"]), robot, Path(route["destination"]))
                else:
                    enqueue(project, source, robot, destination)
                for path in (home / "acks").glob("*.json"):
                    if path.stem not in seen:
                        receipt = read(path)
                        log(f"[自动传输] 6000d已校验入库：global_episode={receipt['global_episode']:06d} key={path.stem[:12]}")
                        seen.add(path.stem)
                        payload = home / "outbox" / path.stem / "payload"
                        if payload.is_dir():
                            shutil.rmtree(payload)  # ONLY redundant frozen transport copy, never mocap_data.
                atomic_json(home / "status.json", dict(updated=time.time(), pending=len(outbox_index(project)), error=None))
            except Exception as exc:
                log(f"[自动传输] 暂未完成，将重试：{exc}")
                atomic_json(home / "status.json", dict(updated=time.time(), error=str(exc)))
            time.sleep(3)


def verify_payload(payload, manifest):
    """Verify all immutable files before any destination publication."""
    paths = set()
    for item in manifest["files"]:
        if item["path"] in paths:
            raise ValueError("duplicate payload path")
        paths.add(item["path"])
        path = safe_path(payload, item["path"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"SHA-256/size verification failed: {item['path']}")
    required = set(episode_files(payload, manifest["row"], manifest["info"]))
    if required != paths:
        raise ValueError("manifest is missing required episode assets or includes unexpected files")


def validate_episode(payload, manifest):
    import numpy as np
    import pyarrow.parquet as pq
    from pico_f1.lerobot_v3 import LeRobotV3Dataset, CAMERA_KEYS
    from pico_f1.episode_analysis import episode_paths
    from pico_f1.alignment_archive import read_extras, validate_coverage
    verify_payload(payload, manifest)
    info, row = manifest["info"], manifest["row"]
    if info["codebase_version"] != "v3.0" or info["fps"] != 30 or info["robot_type"] != "f1_omnipicker":
        raise ValueError("expected F1 OmniPicker LeRobot v3.0 at 30 fps")
    data, videos = episode_paths(payload, row, info)
    table = pq.read_table(data)
    if table.schema != LeRobotV3Dataset._schema() or len(table) != row["length"] or len(table) < 2:
        raise ValueError("episode schema/length mismatch")
    for field in ("observation.state", "action"):
        if not np.isfinite(np.asarray(table[field].to_pylist())).all():
            raise ValueError("nonfinite state/action")
    for field in ("source.timestamp_ns", "source.capture_monotonic_ns"):
        if not (np.diff(table[field].to_numpy()) > 0).all():
            raise ValueError("nonmonotonic source timestamps")
    if not np.array_equal(table["frame_index"].to_numpy(), np.arange(len(table))):
        raise ValueError("missing/misordered frame indices")
    if not np.allclose(table["timestamp"].to_numpy(), np.arange(len(table))/30, atol=1e-4):
        raise ValueError("incorrect nominal 30 fps timestamps")
    if not (table["episode_index"].to_numpy() == manifest["source_episode"]).all():
        raise ValueError("wrong source episode")
    extras = read_extras(payload / "alignment" / f"episode-{manifest['source_episode']:06d}", verify=True)
    validate_coverage(np.asarray(table["source.camera_timestamp_ns"].to_pylist()), extras, 55_000_000)
    for key, video in zip(CAMERA_KEYS, videos):
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames,r_frame_rate,width,height", "-of", "json", str(video)], timeout=300))
        stream = probe["streams"][0]
        num, den = map(float, stream["r_frame_rate"].split("/"))
        shape = info["features"][key]["shape"]
        if int(stream["nb_read_frames"]) != len(table) or abs(num/den-30) > .001 or [stream["height"], stream["width"]] != shape[:2]:
            raise ValueError("video frame count/fps/shape mismatch")
    return table


def destination_path(value, base):
    """Accept only <allowed base>/<scene>/mocap_data; preserve the public symlink."""
    value = Path(os.path.abspath(value))
    # Missing/unmounted dataset base is an error, never silently create a new
    # directory on the system disk. Scene symlinks cannot redirect writes.
    base = Path(base).resolve(strict=True)
    if value.name != "mocap_data" or value.parent.is_symlink() or value.parent.resolve().parent != base:
        raise ValueError("destination must be <allowed-base>/<scene>/mocap_data")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value.parent.name):
        raise ValueError("scene name must use letters, digits, _ or -")
    return value


def check_reset_generation(dest, manifest):
    """Central-only tombstone: old outboxes must not resurrect a reset scene."""
    marker = Path(dest).parent / '.collection-reset.json'
    if not marker.exists():
        return
    policy = read(marker)
    import math
    queued = manifest.get('queued_at')
    if (str(manifest.get('dataset_uuid')) in policy['blocked_dataset_uuids'] or
            type(queued) not in (int, float) or not math.isfinite(queued) or
            queued <= policy['reset_at']):
        raise ValueError('SCENE_RESET_OLD_BATCH: 中央场景已重置，拒绝旧批次；请检查机器人旧传输队列/时钟，不删除中央重置标记')


def merge(payload, manifest, base=DEFAULT_BASE, fail_at=None):
    """Publish one immutable revision. Retrying a published key returns its original ID."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from pico_f1.lerobot_v3 import CAMERA_KEYS, _read_episode_rows, _merge_stats
    from pico_f1.episode_analysis import episode_paths
    key = key_for(manifest["robot_id"], manifest["dataset_uuid"], manifest["source_episode"])
    if key != manifest["key"] or manifest["row"]["episode_index"] != manifest["source_episode"]:
        raise ValueError("invalid source identity")
    dest = destination_path(manifest["destination"], base)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    home = dest.parent / ".mocap_data-transfer"
    home.mkdir(exist_ok=True, mode=0o700)
    with lock(home / "merge.lock"):
        check_reset_generation(dest, manifest)
        old = None
        if dest.is_symlink():
            old = dest.resolve(strict=True)
            if old.parent != (home / "revisions").resolve():
                raise ValueError("destination symlink is not managed by this merger")
        elif dest.exists() and any(dest.iterdir()):
            raise ValueError("destination is a nonempty unmanaged dataset; refusing overwrite")
        prior = read(old / "provenance.json") if old else dict(schema_version=1, episodes=[])
        for record in prior["episodes"]:
            if record["key"] == key:
                if record["manifest_sha256"] != object_hash(manifest):
                    raise ValueError("same source identity with different content; quarantined")
                return record
        table = validate_episode(payload, manifest)
        source_info = manifest["info"]
        old_info = read(old / "meta/info.json") if old else None
        if old_info:
            for field in ("features", "fps", "robot_type", "codebase_version", "camera_integrity_basis", "camera_sync_basis"):
                if old_info.get(field) != source_info.get(field):
                    raise ValueError(f"incompatible dataset contract: {field}")
        if shutil.disk_usage(home).free < max(5*1024**3, 2*sum(x["size"] for x in manifest["files"])):
            raise ValueError("6000d空间不足，未发布数据")
        global_episode = int(old_info["total_episodes"]) if old_info else 0
        global_index = int(old_info["total_frames"]) if old_info else 0
        revision = home / "revisions" / (f"{global_episode:06d}-" + key[:12] + "-" + uuid.uuid4().hex[:8])
        revision.parent.mkdir(exist_ok=True)
        if old:
            shutil.copytree(old, revision, copy_function=os.link)
        else:
            revision.mkdir()
        # All mutations of hardlinked metadata use replace, never in-place writes.
        def parquet(path, data):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".new")
            pq.write_table(data, tmp)
            os.replace(tmp, path)
        tasks = pq.read_table(old / "meta/tasks.parquet").to_pylist() if old else []
        task_ids = {r["task"]: int(r["task_index"]) for r in tasks}
        mapping = {}
        for task in manifest["tasks"]:
            name = task["task"]
            if name not in task_ids:
                task_ids[name] = len(tasks)
                tasks.append(dict(task_index=len(tasks), task=name))
            mapping[int(task["task_index"])] = task_ids[name]
        columns = {"episode_index": [global_episode]*len(table),
                   "index": list(range(global_index, global_index+len(table))),
                   "task_index": [mapping[int(i)] for i in table["task_index"].to_pylist()]}
        for name, values in columns.items():
            index = table.schema.get_field_index(name)
            table = table.set_column(index, table.schema.field(index), pa.array(values, type=pa.int64()))
        chunk, file_index = divmod(global_episode, 1000)
        row = dict(manifest["row"], episode_index=global_episode)
        row.update({"meta/episodes/chunk_index": chunk, "meta/episodes/file_index": file_index,
                    "data/chunk_index": chunk, "data/file_index": file_index,
                    "dataset_from_index": global_index, "dataset_to_index": global_index + len(table)})
        for camera in CAMERA_KEYS:
            row[f"videos/{camera}/chunk_index"] = chunk
            row[f"videos/{camera}/file_index"] = file_index
        data_path, videos = episode_paths(revision, row, source_info)
        _, source_videos = episode_paths(payload, manifest["row"], source_info)
        parquet(data_path, table)
        for target, original in zip(videos, source_videos):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError("refusing to overwrite existing episode video")
            shutil.copyfile(original, target)
            if digest(target) != digest(original):
                raise ValueError("central video copy hash mismatch")
        align = payload / "alignment" / f"episode-{manifest['source_episode']:06d}"
        if align.exists():
            shutil.copytree(align, revision / "alignment" / f"episode-{global_episode:06d}")
        parquet(revision / f"meta/episodes/chunk-{chunk:03d}/file-{file_index:03d}.parquet", pa.Table.from_pylist([row]))
        parquet(revision / "meta/tasks.parquet", pa.Table.from_pylist(tasks))
        rows = _read_episode_rows(revision)
        if len(rows) != global_episode+1 or sum(int(r["length"]) for r in rows) != global_index+len(table):
            raise ValueError("merged index/count mismatch")
        atomic_json(revision / "meta/stats.json", {f: _merge_stats(rows, f) for f in ("observation.state", "action")})
        info = dict(old_info or source_info, total_episodes=global_episode+1, total_frames=global_index+len(table),
                    total_tasks=len(tasks), splits={"train": f"0:{global_episode+1}"})
        receipt = dict(key=key, manifest_sha256=object_hash(manifest), destination=str(dest),
                       global_episode=global_episode, robot_id=manifest["robot_id"],
                       dataset_uuid=manifest["dataset_uuid"], source_episode=manifest["source_episode"],
                       source_root=manifest["source_root"], source_files=manifest["files"], received_at=time.time(),
                       central_data_path=str(data_path.relative_to(revision)), central_data_sha256=digest(data_path))
        prior["episodes"].append(receipt)
        atomic_json(revision / "provenance.json", prior)
        atomic_json(revision / "meta/info.json", info)
        # Durable bytes and directory entries before publishing/ACKing.
        for path in revision.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for directory in sorted((p for p in revision.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            fsync_dir(directory)
        fsync_dir(revision)
        fsync_dir(revision.parent)
        if fail_at == "before_publish":
            raise RuntimeError("injected crash before publication")
        if dest.exists() and not dest.is_symlink():
            # Only a verified empty directory; preserved for audit, not deleted.
            os.rename(dest, home / ("initial-empty-" + uuid.uuid4().hex))
        next_link = dest.with_name(".mocap_data-next-" + uuid.uuid4().hex)
        next_link.symlink_to(revision)
        os.replace(next_link, dest)
        fsync_dir(dest.parent)
        if fail_at == "after_publish":
            raise RuntimeError("injected crash after publication before ACK")
        log(f"[自动传输] 完整校验并入库：{manifest['robot_id']} 本地{manifest['source_episode']:06d} → 中央{global_episode:06d}，{len(table)}帧；{dest}")
        return receipt


def ssh(robot, command, stdin=None):
    return subprocess.check_output(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                   "-o", "StrictHostKeyChecking=yes", robot, command], input=stdin, timeout=60,
                                   stderr=subprocess.PIPE)


def receive_robot(robot, cache, base):
    prefix = f"test \"$(cat /home/user/.f1_robot_id)\" = {robot} && {REMOTE_PROJECT}/.venv/bin/python {REMOTE_PROJECT}/scripts/transfer_probe.py "
    ready = f"test -x {REMOTE_PROJECT}/.venv/bin/python && test -f {REMOTE_PROJECT}/scripts/transfer_probe.py || exit 42; "
    try:
        items = json.loads(ssh(robot, ready + prefix + "index"))
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 42:
            raise ValueError("尚未安装v1.4.2自动传输组件，等待该机器人接入") from exc
        raise
    completed = 0
    errors = []
    for item in items:
        leases = cache / "collection_leases.json"
        if leases.exists() and robot not in read(leases):
            break
        try:
            receive_item(robot, cache, base, prefix, item)
            completed += 1
        except Exception as exc:
            # A damaged old episode must not block healthy new recordings.
            errors.append(f"{item.get('key', '?')[:12]}: {exc}")
    if errors:
        raise ValueError(f"本轮已完成{completed}条；待重试：" + "; ".join(errors))
    return completed


def receive_item(robot, cache, base, prefix, item):
    # Kept separate so each failed episode is isolated and retried independently.
    if (cache/'scene_reset_in_progress.json').exists():
        raise ValueError('6000d场景重置未完成，暂停接收')
    key = item["key"]
    if not re.fullmatch("[0-9a-f]{64}", key):
        raise ValueError("unsafe outbox key")
    manifest = json.loads(ssh(robot, prefix + "manifest " + key))
    if object_hash(manifest) != item["sha256"] or manifest["robot_id"] != robot:
        raise ValueError("outbox identity/hash mismatch")
    if Path(base) == DEFAULT_BASE:
        from pico_f1.collection_control import allowed_destination
        base = allowed_destination(manifest["destination"]).parent.parent
    else:
        destination_path(manifest["destination"], base)
    check_reset_generation(Path(manifest['destination']), manifest)
    stage = cache / "incoming" / key
    stage.mkdir(parents=True, exist_ok=True)
    atomic_json(stage / "manifest.json", manifest)
    payload = stage / "payload"
    payload.mkdir(exist_ok=True)
    if shutil.disk_usage(cache).free < max(5*1024**3, 2*sum(x["size"] for x in manifest["files"])):
        raise ValueError("receiver cache disk space insufficient")
    filelist = stage / "files.txt"
    paths = []
    for entry in manifest["files"]:
        safe_path(payload, entry["path"])
        paths.append(entry["path"])
    filelist.write_text("\n".join(paths)+"\n")
    subprocess.run(["rsync", "-rt", "--checksum", "--partial", "--timeout=30", "--bwlimit=8192",
                    "--files-from="+str(filelist), "-e", "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes",
                    f"{robot}:{REMOTE_PROJECT}/transfer_state/outbox/{key}/payload/", str(payload)+"/"], check=True, timeout=1800)
    receipt = merge(payload, manifest, base)
    # ACK only after durable atomic publication; lost ACK is idempotent.
    ssh(robot, prefix + "ack", encoded(receipt))
    atomic_json(cache / "receipts" / (key+".json"), receipt)
    shutil.rmtree(payload)  # Received transport copy only; published revision is independent.


def receiver(cache, base, robots, once=False):
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock(cache / "receiver.lock", nonblock=True):
        log(f"[自动传输] 6000d接收服务已启动；SSH机器人={','.join(robots)}；允许数据集路径={base}")
        if not once:
            def watchdog():
                from pico_f1.collection_control import expire_leases
                while True:
                    time.sleep(5)
                    try:
                        expire_leases(Path(__file__).resolve().parents[2])
                    except Exception as exc:
                        log(f"[数采会话] 超时检查失败：{exc}")
            threading.Thread(target=watchdog, name="collection-watchdog", daemon=True).start()
        def worker(robot):
            last_error, last_log = None, 0.0
            while True:
                delay = 5
                try:
                    leases = cache / "collection_leases.json"
                    if not once and leases.exists() and robot not in read(leases):
                        time.sleep(5)
                        continue
                    count = receive_robot(robot, cache, base)
                    atomic_json(cache / f"{robot}.status.json", dict(updated=time.time(), error=None, completed=count))
                    last_error = None
                except Exception as exc:
                    if str(exc) != last_error or time.monotonic() - last_log >= 60:
                        log(f"[自动传输] {robot}暂未完成，将重试：{exc}")
                        last_log = time.monotonic()
                    last_error = str(exc)
                    atomic_json(cache / f"{robot}.status.json", dict(updated=time.time(), error=str(exc)))
                    delay = 15
                if once:
                    return
                time.sleep(delay)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(robots)) as pool:
            list(pool.map(worker, robots))


def main():
    os.umask(0o077)
    os.nice(10)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("agent", "receiver"))
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--robot", choices=ROBOTS)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--robots", nargs="+", choices=ROBOTS, default=list(ROBOTS))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.mode == "agent":
        identity = Path("/home/user/.f1_robot_id").read_text().strip()
        if identity != args.robot or identity not in ROBOTS:
            raise SystemExit("ROBOT_IDENTITY_MISMATCH")
        source = args.source or args.project / "mocap_data"
        if args.once:
            with lock(args.project / "transfer_state/agent.lock", nonblock=True):
                enqueue(args.project, source, identity, args.destination)
        else:
            agent(args.project, source, identity, args.destination)
    else:
        receiver(args.cache or args.project.parent / "transfer_6000d", args.base, args.robots, args.once)


if __name__ == "__main__":
    main()
