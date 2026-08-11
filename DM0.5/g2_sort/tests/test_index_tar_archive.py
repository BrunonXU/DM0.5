"""验证 tar.gz 单遍索引的必需文件检查、排除原因和提前停止。"""

import io
import json
import tarfile

from g2_sort.data_tools import index_tar_archive as MODULE


def _add_bytes(archive, name, payload=b"x"):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _add_episode(archive, name, meta, missing=()):
    root = f"batch/{name}"
    _add_bytes(
        archive,
        f"{root}/meta_info.json",
        json.dumps(meta).encode(),
    )
    for key, suffix in MODULE.REQUIRED_MEMBERS.items():
        if key not in missing:
            _add_bytes(archive, f"{root}/{suffix}")


def test_index_tar_reports_valid_and_invalid_episodes(tmp_path):
    archive_path = tmp_path / "episodes.tar.gz"
    valid_meta = {
        "AID": "g2",
        "sw_version": "2.2.0",
        "task_id": 5,
        "duration": 37,
        "data_validate": {"validate": True},
        "integrity": {"integrity": True},
        "fps_validate": True,
    }
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_episode(archive, "episode_ok", valid_meta)
        _add_episode(
            archive,
            "episode_bad",
            {**valid_meta, "data_validate": {"validate": False}},
            missing=("right_wrist",),
        )

    report = MODULE.index_tar(archive_path)

    assert report["episode_count"] == 2
    assert report["precheck_valid_count"] == 1
    assert report["stopped_early"] is False
    invalid = report["episodes"][1]
    assert invalid["exclusion_reasons"] == [
        "data_validate_false",
        "missing_right_wrist",
    ]


def test_index_tar_can_stop_after_complete_sample(tmp_path):
    archive_path = tmp_path / "episodes.tar.gz"
    meta = {
        "data_validate": {"validate": True},
        "integrity": {"integrity": True},
        "fps_validate": True,
    }
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_episode(archive, "episode_1", meta)
        _add_episode(archive, "episode_2", meta)

    report = MODULE.index_tar(archive_path, max_episodes=1)

    assert report["episode_count"] == 1
    assert report["precheck_valid_count"] == 1
    assert report["stopped_early"] is True
    assert report["episodes"][0]["episode"] == "episode_1"
