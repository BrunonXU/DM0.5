from __future__ import annotations

from g2teleop.model_audit import missing_names, sha256_file


def test_sha256_file(tmp_path) -> None:
    sample = tmp_path / "sample"
    sample.write_bytes(b"abc")
    assert sha256_file(sample) == (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )


def test_missing_names_preserves_expected_order() -> None:
    assert missing_names(["a", "c"], ["a", "b", "c", "d"]) == ["b", "d"]
