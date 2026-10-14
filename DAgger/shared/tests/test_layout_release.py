"""Exercise the source layout and actual release trees without robot imports."""
from hashlib import sha256
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def script_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_python(arguments, cwd, *, source=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    if source:
        env["PYTHONPATH"] = str(source)
    return subprocess.run([sys.executable, *arguments], cwd=cwd, env=env,
                          capture_output=True, text=True, check=True)


@pytest.fixture
def release(tmp_path):
    packer = script_module(ROOT / "F1/scripts/53_package_f1.py", "f1_packer")
    archive = tmp_path / "f1.tar.gz"
    result = packer.package(archive)
    stage = script_module(ROOT / "F1/scripts/58_stage_f1.py", "f1_stage")
    manifest = stage.inspect_package(archive)
    extracted = tmp_path / "another machine with spaces"
    extracted.mkdir()
    with tarfile.open(archive) as handle:
        # inspect_package has already rejected links and paths outside DAgger.
        for member in handle.getmembers():
            destination = extracted / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(handle.extractfile(member).read())
    return archive, extracted / "DAgger", manifest, result


def test_f1_release_is_flat_complete_and_has_only_docs(release):
    archive, unpacked, manifest, result = release
    names = set(manifest["files"])
    assert "src/dagger_core/pink_ik.py" in names
    assert "src/f1dagger/cli.py" in names
    assert "docs/README.md" in names
    assert not any(name.startswith(("doc/", "F1/", "G2/", "shared/", "src/g2dagger/")) for name in names)
    assert result["files"] == len(names)
    assert "dagger-core==" not in (unpacked / "pyproject.toml").read_text()
    assert "](docs/STATUS.md)" in (unpacked / "README.md").read_text()
    with tarfile.open(archive) as handle:
        assert len(handle.getmembers()) == len(names) + 1
        for member in handle.getmembers():
            assert member.isfile()
            if member.name != "DAgger/F1_RELEASE_MANIFEST.json":
                assert sha256(handle.extractfile(member).read()).hexdigest() == manifest["files"][member.name.removeprefix("DAgger/")]


def test_unpacked_f1_imports_and_identity_need_no_workspace(release, tmp_path):
    _, unpacked, _, _ = release
    manifest_hash = sha256((unpacked / "F1_RELEASE_MANIFEST.json").read_bytes()).hexdigest()
    code = '''
import json, sys
from pathlib import Path
class NoOtherPlatform:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('g2dagger', 'g2teleop', 'g2_sort', 'rclpy'):
            raise AssertionError('unexpected runtime dependency: '+fullname)
sys.meta_path.insert(0, NoOtherPlatform())
from f1dagger.identity import runtime_identity
from f1dagger.policy import F1PolicyEngine
from f1dagger.teleop import F1ArmTarget
from f1dagger.network import IntentServer
import dagger_core
root=Path(sys.argv[1])
assert root in Path(dagger_core.__file__).parents
print(json.dumps(runtime_identity({'_config_sha256':'a'*64,
    'accepted_release_manifest_sha256':sys.argv[2]}, root=root, powered=True)))
'''
    result = run_python(["-c", code, str(unpacked), manifest_hash], tmp_path, source=unpacked / "src")
    from f1dagger.identity import runtime_identity
    source_identity = runtime_identity({"_config_sha256": "a" * 64}, root=ROOT / "F1")
    assert json.loads(result.stdout)["source_sha256"] == source_identity["source_sha256"]
    run_python(["-m", "f1dagger", "--help"], tmp_path, source=unpacked / "src")
    # A neighboring source checkout must never override the bundled pinned core.
    unrelated_core = unpacked.parent / "shared/src/dagger_core"
    unrelated_core.mkdir(parents=True)
    (unrelated_core / "__init__.py").write_text("raise AssertionError('external core was imported')\n")
    run_python([str(unpacked / "scripts/63_f1_shadow_soak.py"), "--help"], tmp_path)


def test_stage_rejects_unmanifested_doc_alias(release, tmp_path):
    archive, _, _, _ = release
    modified = tmp_path / "extra-doc.tar.gz"
    with tarfile.open(archive) as source, tarfile.open(modified, "w:gz") as target:
        for member in source.getmembers():
            target.addfile(member, source.extractfile(member))
        alias = tarfile.TarInfo("DAgger/doc/README.md")
        data = b"unbound alias"
        alias.size = len(data)
        target.addfile(alias, BytesIO(data))
    stage = script_module(ROOT / "F1/scripts/58_stage_f1.py", "f1_stage_extra")
    with pytest.raises(ValueError, match="unbound archive files"):
        stage.inspect_package(modified)


def test_windows_sender_bundle_includes_shared_implementation(tmp_path):
    output = tmp_path / "isolated windows sender"
    run_python([str(ROOT / "G2/scripts/38_prepare_windows_hil_shadow.py"), str(output)], tmp_path)
    assert (output / "src/dagger_core/async_transport.py").is_file()
    assert (output / "src/g2dagger/paths.py").is_file()
    manifest = json.loads((output / "manifest.json").read_text())
    for relative, digest in manifest["files_sha256"].items():
        assert sha256((output / relative).read_bytes()).hexdigest() == digest
    run_python([str(output / "scripts/03_windows_shadow_sender.py"), "--help"], tmp_path)


def test_g2_artifacts_resolve_for_workspace_flat_and_override(tmp_path, monkeypatch):
    from g2dagger.paths import artifact_root
    monkeypatch.delenv("DAGGER_ARTIFACTS_ROOT", raising=False)
    assert artifact_root(ROOT / "G2") == ROOT / "artifacts"
    flat = tmp_path / "remote" / "DAgger"
    assert artifact_root(flat) == flat
    monkeypatch.setenv("DAGGER_ARTIFACTS_ROOT", str(tmp_path / "custom outputs"))
    assert artifact_root(ROOT / "G2") == tmp_path / "custom outputs"
    assert artifact_root(flat) == tmp_path / "custom outputs"


def test_f1_repack_prefers_bundled_core_and_docs_over_neighbors(release, tmp_path):
    _, unpacked, original_manifest, _ = release
    neighboring_core = unpacked.parent / "shared/src/dagger_core"
    neighboring_core.mkdir(parents=True)
    (neighboring_core / "__init__.py").write_text("raise AssertionError('external core')\n")
    neighboring_docs = unpacked.parent / "docs/F1"
    neighboring_docs.mkdir(parents=True)
    (neighboring_docs / "README.md").write_text("unrelated documentation\n")
    repacked = tmp_path / "repacked.tar.gz"
    run_python([str(unpacked / "scripts/53_package_f1.py"), "--output", str(repacked)], tmp_path)
    stage = script_module(ROOT / "F1/scripts/58_stage_f1.py", "f1_stage_repacked")
    assert stage.inspect_package(repacked)["files"] == original_manifest["files"]
