from dataclasses import dataclass
import hashlib
from io import BytesIO
import json

from PIL import Image, UnidentifiedImageError
import numpy as np
import pytest

from f1dagger.rtc_pixels import PixelEncoder


@dataclass(frozen=True)
class Frame:
    camera: str
    payload: bytes
    frame_id: int
    timestamp: int
    media_type: str = 'image/jpeg'
    width: int = 320
    height: int = 240


RIGHT_ONLY = dict(schema='f1_toys_eef_contract_v2', contract='f1_upper_16d_v1',
                  input_version='right_only_v1', cameras=['head', 'left_wrist', 'right_wrist'],
                  active_cameras=['head', 'right_wrist'], image_size=224,
                  active_state_indices=[7,8,9,10,11,12,13,15], state_dim=16)


def encoder(tmp_path, workers, *, contract=None, masked_left_transport=False):
    (tmp_path / 'processing.py').write_text(
        'import numpy as np\n'
        'def process_image(image, camera, cfg):\n'
        '    return np.asarray(image.convert("RGB").resize((224, 224)))\n')
    (tmp_path / 'contract.json').write_text(json.dumps({} if contract is None else contract))
    identity = {key: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
                for key, name in (('implementation_sha256', 'processing.py'),
                                  ('contract_sha256', 'contract.json'))}
    return PixelEncoder(tmp_path, identity, workers=workers, masked_left_transport=masked_left_transport)


def frames():
    result = []
    for i, camera in enumerate(('head', 'left_wrist', 'right_wrist')):
        stream = BytesIO()
        Image.new('RGB', (320, 240), (10+i*20, 30, 80)).save(stream, format='JPEG')
        result.append(Frame(camera, stream.getvalue(), i+40, i+1000))
    return tuple(result)


def test_parallel_preserves_bytes_order_identity_and_can_reopen(tmp_path):
    serial, parallel = encoder(tmp_path, 1), encoder(tmp_path, 3)
    source = frames()
    expected = serial.encode(source)
    try:
        for _ in range(3):
            actual = parallel.encode(source)
            assert actual == expected
            assert [f.frame_id for f in actual] == [f.frame_id for f in source]
            assert [f.timestamp for f in actual] == [f.timestamp for f in source]
            parallel.close()
            assert parallel.executor is None
    finally:
        serial.close()
        parallel.close()


def test_parallel_decode_failure_propagates_and_cleanup_joins(tmp_path):
    parallel = encoder(tmp_path, 3)
    try:
        with pytest.raises(UnidentifiedImageError):
            parallel.encode((Frame('head', b'broken JPEG', 1, 2), *frames()[1:]))
    finally:
        parallel.close()
    assert parallel.executor is None


@pytest.mark.parametrize('workers', [0, 4, True, 1.5, '3'])
def test_invalid_worker_count(tmp_path, workers):
    with pytest.raises(ValueError, match='workers'):
        encoder(tmp_path, workers)


@pytest.mark.parametrize('workers', [1, 3])
def test_masked_transport_preserves_active_bytes_source_and_frame_identity(tmp_path, workers):
    baseline = encoder(tmp_path, workers, contract=RIGHT_ONLY)
    masked = encoder(tmp_path, workers, contract=RIGHT_ONLY, masked_left_transport=True)
    source = frames()
    original_payloads = tuple(frame.payload for frame in source)
    try:
        reference = baseline.encode(source)
        for _ in range(2):
            result = masked.encode(source)
            assert result[0] == reference[0] and result[2] == reference[2]
            assert tuple(frame.payload for frame in source) == original_payloads
            assert [frame.camera for frame in result] == list(RIGHT_ONLY['cameras'])
            assert [(frame.frame_id, frame.timestamp) for frame in result] == [(frame.frame_id, frame.timestamp) for frame in source]
            assert len(result[1].payload) == 738
            assert hashlib.sha256(result[1].payload).hexdigest() == masked.transport_identity['png_sha256']
            with Image.open(BytesIO(result[1].payload)) as image:
                assert image.size == (224, 224) and image.mode == 'RGB'
                assert not np.asarray(image).any()
            masked.close()
        assert masked.transport_identity['contract_sha256'] == masked.identity['contract_sha256']
        changed = masked.transport_identity
        changed['constant_cameras'].append('head')
        assert masked.transport_identity['constant_cameras'] == ['left_wrist']
    finally:
        baseline.close()
        masked.close()


def test_masked_slot_skips_processing_but_active_decode_failure_remains_fatal(tmp_path):
    masked = encoder(tmp_path, 3, contract=RIGHT_ONLY, masked_left_transport=True)
    try:
        source = frames()
        result = masked.encode((source[0], Frame('left_wrist', b'not decoded', 77, 88), source[2]))
        assert result[1].frame_id == 77 and result[1].timestamp == 88
        with pytest.raises(UnidentifiedImageError):
            masked.encode((Frame('head', b'broken JPEG', 1, 2), *source[1:]))
    finally:
        masked.close()


@pytest.mark.parametrize('change', [
    {'input_version': None}, {'input_version': 'legacy'},
    {'active_cameras': ['head', 'left_wrist', 'right_wrist']},
    {'active_cameras': ['head']}, {'image_size': 256},
    {'cameras': ['head', 'right_wrist']}, {'state_dim': 32},
    {'schema': 'f1_toys_contract_v1'}, {'active_state_indices': list(range(16))},
])
def test_masked_mode_rejects_legacy_or_mismatched_contract(tmp_path, change):
    with pytest.raises(ValueError, match='right_only_v1'):
        encoder(tmp_path, 3, contract=dict(RIGHT_ONLY, **change), masked_left_transport=True)


@pytest.mark.parametrize('value', [None, 0, 1, 'true'])
def test_masked_switch_requires_explicit_boolean(tmp_path, value):
    with pytest.raises(ValueError, match='boolean'):
        encoder(tmp_path, 3, contract=RIGHT_ONLY, masked_left_transport=value)


def test_masked_mode_rejects_missing_reordered_frames_and_changed_contract(tmp_path):
    masked = encoder(tmp_path, 3, contract=RIGHT_ONLY, masked_left_transport=True)
    source = frames()
    try:
        for invalid in (source[:2], tuple(reversed(source)), (source[0], source[0], source[2])):
            with pytest.raises(ValueError, match='three ordered'):
                masked.encode(invalid)
        masked.contract['active_cameras'] = list(RIGHT_ONLY['cameras'])
        with pytest.raises(ValueError, match='right_only_v1'):
            masked.encode(source)
    finally:
        masked.close()
