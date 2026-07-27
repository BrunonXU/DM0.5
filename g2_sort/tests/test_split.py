"""防止95/5划分出现episode泄漏、非确定性或错误数量。"""

from g2_sort.data_tools import build_split as MODULE


def test_split_is_episode_disjoint_and_deterministic():
    reports = [
        {
            "episode": f"episode_{index:03d}",
            "valid": index < 97,
            "aid": "g2",
            "software_version": "2.2",
            "task_id": 4,
        }
        for index in range(100)
    ]

    first = MODULE.build_split(reports, 0.05, "fixed-seed")
    second = MODULE.build_split(reports, 0.05, "fixed-seed")

    assert first == second
    assert len(first["train"]) == 92
    assert len(first["validation"]) == 5
    assert len(first["excluded"]) == 3
    assert set(first["train"]).isdisjoint(first["validation"])
