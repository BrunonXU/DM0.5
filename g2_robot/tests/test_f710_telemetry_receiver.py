from __future__ import annotations

from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/receive_g2_telemetry.sh"


def _receiver(tmp_path: Path, category: str, filename: str) -> subprocess.CompletedProcess:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copied = scripts / SCRIPT.name
    copied.write_bytes(SCRIPT.read_bytes())
    return subprocess.run(
        ["bash", str(copied), "telemetry-put", category, filename],
        input=b'{"event":"runtime_summary"}\n',
        capture_output=True,
        check=False,
    )


def test_receiver_accepts_strict_shadow_f710_name(tmp_path) -> None:
    result = _receiver(
        tmp_path,
        "shadow",
        "g2_shadow_f710_ruckig_20260901_170100.jsonl",
    )
    assert result.returncode == 0, result.stderr.decode()
    stored = (
        tmp_path
        / "logs/telemetry/shadow/g2_shadow_f710_ruckig_20260901_170100.jsonl"
    )
    assert stored.read_bytes() == b'{"event":"runtime_summary"}\n'


def test_receiver_rejects_category_filename_mismatch(tmp_path) -> None:
    result = _receiver(
        tmp_path,
        "powered",
        "g2_shadow_f710_ruckig_20260901_170100.jsonl",
    )
    assert result.returncode == 64
    assert b"invalid F710 telemetry filename" in result.stderr
