"""The collection must not silently substitute an episode or approval version."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("prepare_collection_indexes", ROOT / "prepare_collection_indexes.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sources():
    return ({"episodes": [{"run": "episode_a", "manifest_sha256": "hash_a"},
                          {"run": "episode_b", "manifest_sha256": "hash_b"}]},
            {"scope": [{"episode": "episode_a", "manifest_sha256": "hash_a"},
                       {"episode": "episode_b", "manifest_sha256": "hash_b"}]})


def test_exact_confirmation_keeps_episode_identity():
    catalog, confirmation = sources()
    original = deepcopy((catalog, confirmation))
    assert module.validate_sources(catalog, confirmation) == catalog["episodes"]
    assert (catalog, confirmation) == original


@pytest.mark.parametrize("mutation", ["hash", "missing", "extra", "duplicate", "unsafe"])
def test_collection_rejects_wrong_or_ambiguous_confirmation(mutation):
    catalog, confirmation = sources()
    if mutation == "hash":
        confirmation["scope"][0]["manifest_sha256"] = "different_version"
    elif mutation == "missing":
        confirmation["scope"].pop()
    elif mutation == "extra":
        confirmation["scope"].append({"episode": "episode_c", "manifest_sha256": "hash_c"})
    elif mutation == "duplicate":
        catalog["episodes"].append(catalog["episodes"][0])
    else:
        catalog["episodes"][0]["run"] = "../episode_a"
    with pytest.raises(ValueError):
        module.validate_sources(catalog, confirmation)
