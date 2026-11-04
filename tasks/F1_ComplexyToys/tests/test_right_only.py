import copy
from pathlib import Path
import sys

import numpy as np
from PIL import Image
import pytest

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))
from f1_toys.processing import ACTIVE, CAMERA_KEYS, Processor, load_contract, process_image
from f1_toys.inference import Policy


class StateTokenizer:
    def tokenize(self, prompt, state):
        return np.round(state * 100).astype(np.int64), np.ones(32, bool)


def inputs():
    cfg = load_contract(TASK/'configs/eef_v1_dagger_right_only.json')
    stats = {'state': {'low': [-100]*14+[0,0], 'high': [100]*16}}
    p = Processor(cfg, stats, StateTokenizer())
    images = {c: Image.new('RGB', (1280,720) if c == 'head' else (640,480), (80,130,210))
              for c in CAMERA_KEYS}
    return p, images, np.arange(16, dtype=float)


def equal(a, b):
    for k in a:
        if isinstance(a[k], dict):
            for c in a[k]:
                np.testing.assert_array_equal(a[k][c], b[k][c])
        else:
            np.testing.assert_array_equal(a[k], b[k])


@pytest.mark.parametrize('seed', [None, 713])
def test_left_perturbation_never_reaches_model_even_with_augmentation(seed):
    p, images, state = inputs()
    a = p.observation(images, state, augmentation_seed=seed)
    changed = state.copy(); changed[:7] = [-91,23,-47,60,18,-30,40]; changed[14] = 100
    images['left_wrist'] = Image.new('RGB',(640,480),(255,0,255))
    b = p.observation(images, changed, augmentation_seed=seed)
    equal(a,b)
    inactive = [i for i in range(32) if i not in ACTIVE]
    assert not a['state'][inactive].any()
    assert a['image_mask'] == {key: c!='left_wrist' for c,key in CAMERA_KEYS.items()}
    assert (a['image']['left_wrist_0_rgb'] == -1).all()
    assert not np.array_equal(a['state'][list(ACTIVE)], np.zeros(8))
    np.testing.assert_array_equal(state, np.arange(16))


def test_prepared_pixels_path_and_missing_left_share_exact_processor():
    p, images, state = inputs()
    a = p.observation(images,state)
    del images['left_wrist']
    equal(a,p.observation(images,state))
    pixels = {c:process_image(im,c,p.contract) for c,im in images.items()}
    policy = object.__new__(Policy);policy.processor=p
    equal(a,policy.observation_from_pixels(pixels,state))
    pixels['head'] = np.zeros((1,1,3),np.uint8)
    with pytest.raises(ValueError,match='Prepared pixels'):
        policy.observation_from_pixels(pixels,state)
    with pytest.raises(ValueError,match='active'):
        p.observation({'head':images['head']},state)


def test_input_version_cannot_silently_change_masks(tmp_path):
    import json
    p,_,_=inputs()
    bad=copy.deepcopy(p.contract);bad['active_state_indices']=[0,1]
    path=tmp_path/'bad.json';path.write_text(json.dumps(bad))
    with pytest.raises(ValueError,match='input isolation'):
        load_contract(path)
