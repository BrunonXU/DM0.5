import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from inventory_hil_mix import digest, inventory


def dataset(root, episode, split, group=None, synthetic=False):
    root.mkdir(parents=True)
    (root/'samples.jsonl').write_text(json.dumps(dict(episode_id=episode,action_grid_ns=[1,2]))+'\n')
    (root/'manifest.json').write_text(json.dumps(dict(samples=1,synthetic=synthetic,
        dataset_role='correction',prospective_split=split,source_group=group,
        human_correction_reviewed=True,
        source_episodes=[dict(episode=episode,manifest_sha256='hash-'+episode)],
        files_sha256={'samples.jsonl':digest(root/'samples.jsonl')})))
    return root


def test_inventory_counts_and_rejects_leakage_duplicates_and_synthetic(tmp_path):
    baseline=tmp_path/'v1'
    dataset(baseline/'train','old-train','train')
    dataset(baseline/'validation','old-val','validation')
    first=dataset(tmp_path/'first','new1','train','session')
    second=dataset(tmp_path/'second','new2','validation','session')
    result=inventory(baseline,[first])
    assert result['counts']['correction']['train']==dict(episodes=1,windows=1,valid_action_steps=2)
    assert result['counts']['new_demo']['train']['windows']==0
    with pytest.raises(ValueError,match='leaks'):
        inventory(baseline,[first,second])
    with pytest.raises(ValueError,match='Repeated'):
        inventory(baseline,[first,first])
    fake=dataset(tmp_path/'fake','fake','train','fake',synthetic=True)
    with pytest.raises(ValueError,match='Synthetic'):
        inventory(baseline,[fake])
