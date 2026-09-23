"""Contract/regression tests; no model training or robot execution."""
import json
from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from f1_data.reader import F1Dataset, fit_statistics, sha256
from f1_training.data import Release, TrainingDrawSampler, describe_release
from f1_training.evaluation import physical_metrics, validate_selection
from test_reader import make_episode


def release_fixture(tmp_path):
    train = make_episode(tmp_path, 'train')
    val = make_episode(tmp_path, 'val', 'validation', n=120)
    stats = tmp_path / 'stats.json'
    stats.write_text(json.dumps(fit_statistics([train])))
    images = tmp_path / 'images'
    for ep in (train, val):
        directory = images / ep.episode
        directory.mkdir(parents=True)
        (directory / 'manifest.json').write_text(json.dumps({
            'source_npz_sha256': ep.source_sha256,
            'images': {camera: {str(int(i)): {} for i in ep.starts} for camera in ('head', 'left', 'right')}}))
    doc = describe_release([train, val], stats, images)
    release_path = tmp_path / 'release.json'
    release_path.write_text(json.dumps(doc))
    return release_path, doc


def test_release_denies_candidates_and_sidecar_changes(tmp_path):
    path, doc = release_fixture(tmp_path)
    with pytest.raises(ValueError, match='not training approved'):
        Release(path)
    loaded = Release(path, engineering=True)
    assert len(loaded.episodes) == 2
    sidecar = Path(doc['episodes'][0]['source_metadata']['path'])
    changed = json.loads(sidecar.read_text()); changed['split'] = 'validation'
    sidecar.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='release file changed'):
        Release(path, engineering=True)


def test_release_rejects_different_statistical_population(tmp_path):
    path, doc = release_fixture(tmp_path)
    stats = Path(doc['statistics']['path'])
    data = json.loads(stats.read_text()); data['source_packs'][0]['valid_starts'] -= 1
    stats.write_text(json.dumps(data)); doc['statistics']['sha256'] = sha256(stats)
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='exactly the release training sources'):
        Release(path, engineering=True)


def test_draw_identity_preserves_distribution_and_epoch_resume(tmp_path):
    ds = F1Dataset([make_episode(tmp_path, 'short', n=65), make_episode(tmp_path, 'long', n=800)])
    sampler = TrainingDrawSampler(ds)
    first = list(sampler)
    assert [index for index, _, _ in first] == list(ds.balanced_indices(len(ds), seed=42))
    assert first == list(sampler)
    sampler.set_epoch(2)
    second = list(sampler)
    assert first != second
    recreated = TrainingDrawSampler(ds); recreated.set_epoch(2)
    assert second[13:] == list(recreated)[13:]
    assert all(epoch == 2 and draw == i for i, (_, epoch, draw) in enumerate(second))


def test_physical_metrics_and_invalid_rotations():
    truth = np.zeros((50, 23)); truth[:, [3, 7, 12, 16]] = 1
    pred = truth.copy(); pred[:, 0] = .002; pred[:, 20] = .1
    result = physical_metrics(pred, truth, np.zeros(19))
    assert result['20']['left_position_mm'] == pytest.approx(2)
    assert result['50']['left_rotation_deg_valid_only'] == pytest.approx(0)
    assert result['20']['stopped_base_mae'][0] == pytest.approx(.1)
    pred[0, 3:9] = 0
    result = physical_metrics(pred, truth, np.zeros(19))
    assert result['20']['left_rotation_invalid_steps'] == 1
    pred[1, 0] = np.nan
    result = physical_metrics(pred, truth, np.zeros(19))
    assert result['20']['nonfinite_elements'] == 1
    assert result['20']['left_position_mm'] is None


def test_validation_never_silently_samples_train(tmp_path):
    path, doc = release_fixture(tmp_path)
    release = Release(path, engineering=True)
    with pytest.raises(ValueError, match='windows/seeds are missing'):
        validate_selection(release)
    release.document['evaluation'] = {'diffusion_steps': 10, 'seeds': [42], 'samples': [{'episode': 'train', 'start': 0}]}
    with pytest.raises(ValueError, match='validation sources'):
        validate_selection(release)
