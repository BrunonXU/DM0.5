"""Unit & HTTP roundtrip tests for H-36 full-candidate bucket projection exclusion."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from f1_harness.contracts import FixturePose
from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.sam_seed import SEMANTIC_CANDIDATE_SUMMARY_LIMIT, Sam3SeedProvider
from f1_harness.sam_service import RemoteSam2Segmenter, track_frame
from test_full_development_scene import full_scene, sample
from test_sam_runtime import Sam, sam_config


def _make_candidates(count, *, real_box=(400, 200, 480, 290), real_score=0.973,
                     bucket_score=0.45):
    cands = [dict(index=0, box=list(real_box), area_px=7000, score=real_score, in_scope=True)]
    for i in range(1, count):
        x0 = 10 + (i % 8) * 8
        y0 = 300 + (i // 8) * 15
        cands.append(dict(index=i, box=[x0, y0, x0 + 18, y0 + 20], area_px=250,
                          score=round(bucket_score - 0.005 * (i % 10), 3), in_scope=True))
    return cands


class _DiscoverFake(Sam):
    def __init__(self, candidates, *, total=None, truncated=False, reseed_score=0.99,
                 reseed_confirmed=True, reseed_area=200):
        super().__init__()
        self.candidates = candidates
        self.total = len(candidates) if total is None else total
        self.truncated = truncated
        self.reseed_score = reseed_score
        self.reseed_confirmed = reseed_confirmed
        self.reseed_area = reseed_area
        self.discover_calls = []
        self.track_calls = []

    def discover(self, rgb, **kw):
        self.discover_calls.append(kw)
        empty = np.zeros(rgb.shape[:2], bool)
        meta = dict(
            kw,
            identity_status='unresolved',
            identity_source='semantic',
            score_kind='object_presence_not_mask_iou',
            reseeded=False,
            seed_scope='full_image_semantics',
            seed_backend='sam3',
            seed_reason='semantic_scope_ambiguous_or_empty',
            semantic_candidates=self.total,
            semantic_scoped_candidates=self.total,
            semantic_selected_index=None,
            semantic_candidate_summary=list(self.candidates),
            semantic_candidate_summary_truncated=self.truncated,
        )
        return (empty, 0.0), meta

    def track(self, rgb, **kw):
        self.track_calls.append(kw)
        mask = np.zeros(rgb.shape[:2], bool)
        if self.reseed_confirmed:
            x0, y0, x1, y1 = [int(round(v)) for v in kw['box']]
            mask[max(1, y0):min(rgb.shape[0] - 1, y1), max(1, x0):min(rgb.shape[1] - 1, x1)] = True
            if mask.sum() > self.reseed_area:
                yy, xx = np.nonzero(mask)
                mask[:] = False
                mask[yy[:self.reseed_area], xx[:self.reseed_area]] = True
        meta = dict(
            stream_id=kw['stream_id'],
            object_id=kw['object_id'],
            source_ns=kw['source_ns'],
            identity_status='confirmed' if self.reseed_confirmed else 'unresolved',
            identity_source='semantic',
            score_kind='object_presence_not_mask_iou',
            reseeded=bool(kw.get('reseed')),
            seed_backend='sam3',
            seed_reason='unique_in_current_hint' if self.reseed_confirmed else 'semantic_scope_ambiguous_or_empty',
            semantic_candidates=1 if self.reseed_confirmed else 2,
            semantic_scoped_candidates=1 if self.reseed_confirmed else 2,
        )
        return (mask, self.reseed_score), meta


def _provider(fake, *, min_score=0.9, min_area=100):
    cfg = sam_config()
    cfg['views']['right'].update(
        image_shape=[480, 640, 3],
        prompts={'block': [10, 10, 50, 50]},
        sam_objects=['block'],
        require_explicit_target_prompt=True,
        sam_streaming=True,
        minimum_score=min_score,
        minimum_area_px=min_area,
        prompt_padding_px=20,
        sam_tracking_padding_px=8,
    )
    return ConfiguredMaskProvider(cfg, segmenter={'right': fake})


def _source(ns=1):
    return dict(source_ns=ns, stream={'camera': 'right'})


def test_h36_two_candidates_one_excluded_reseeds_and_confirms():
    fake = _DiscoverFake(_make_candidates(2))
    provider = _provider(fake)
    rgb = np.zeros((480, 640, 3), np.uint8)
    masks = provider.segment_frame(rgb, view='right', source=_source(1),
                                   exclusion_boxes={'block': [0, 0, 196, 480]})
    assert masks['block'].sum() >= 100
    assert len(fake.discover_calls) == 1 and len(fake.track_calls) == 1
    assert fake.track_calls[0]['reseed'] is True
    assert np.array_equal(fake.track_calls[0]['box'], [392.0, 192.0, 488.0, 298.0])
    diag = provider.diagnostic_snapshot()['right']['block']
    assert diag['tracking']['recovery_search_source'] == 'fixture_exclusion_unique_candidate'
    assert diag['tracking']['fixture_exclusion_excluded_candidates'] == 1
    assert diag['fixture_exclusion']['remaining_candidates'] == 1


def test_h36_35_candidates_34_excluded_confirms():
    fake = _DiscoverFake(_make_candidates(35))
    provider = _provider(fake)
    rgb = np.zeros((480, 640, 3), np.uint8)
    masks = provider.segment_frame(rgb, view='right', source=_source(2),
                                   exclusion_boxes={'block': [0, 0, 196, 480]})
    assert masks['block'].sum() >= 100
    diag = provider.diagnostic_snapshot()['right']['block']
    assert diag['tracking']['recovery_search_source'] == 'fixture_exclusion_unique_candidate'
    assert diag['tracking']['fixture_exclusion_excluded_candidates'] == 34
    assert diag['fixture_exclusion']['total_candidates'] == 35
    assert diag['fixture_exclusion']['excluded_candidates'] == 34
    assert diag['fixture_exclusion']['remaining_candidates'] == 1


def test_h36_two_candidates_both_outside_exclusion_still_fails():
    cands = [
        dict(index=0, box=[300, 150, 360, 210], area_px=3000, score=0.96, in_scope=True),
        dict(index=1, box=[420, 200, 480, 260], area_px=3000, score=0.95, in_scope=True),
    ]
    fake = _DiscoverFake(cands)
    provider = _provider(fake)
    with pytest.raises(ValueError, match='mask_uncertain_requires_new_prompt:object=block'):
        provider.segment_frame(np.zeros((480, 640, 3), np.uint8), view='right', source=_source(3),
                               exclusion_boxes={'block': [0, 0, 196, 480]})
    assert len(fake.track_calls) == 0
    diag = provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']
    assert diag['applied'] is True and diag['remaining_candidates'] == 2


def test_h36_no_exclusion_box_unchanged():
    fake = _DiscoverFake(_make_candidates(2))
    provider = _provider(fake)
    with pytest.raises(ValueError, match='mask_uncertain_requires_new_prompt:object=block'):
        provider.segment_frame(np.zeros((480, 640, 3), np.uint8), view='right', source=_source(4))
    assert len(fake.track_calls) == 0


def test_h36_remaining_candidate_below_minimum_score_fails():
    fake = _DiscoverFake(_make_candidates(2, real_score=0.55))
    provider = _provider(fake, min_score=0.9)
    with pytest.raises(ValueError, match='mask_uncertain_requires_new_prompt:object=block'):
        provider.segment_frame(np.zeros((480, 640, 3), np.uint8), view='right', source=_source(5),
                               exclusion_boxes={'block': [0, 0, 196, 480]})
    assert len(fake.track_calls) == 0
    assert provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']['rejected_below_minimum_score'] is True


def test_h36_truncated_summary_disables_exclusion():
    fake = _DiscoverFake(_make_candidates(64), total=65, truncated=True)
    provider = _provider(fake)
    with pytest.raises(ValueError, match='mask_uncertain_requires_new_prompt:object=block'):
        provider.segment_frame(np.zeros((480, 640, 3), np.uint8), view='right', source=_source(6),
                               exclusion_boxes={'block': [0, 0, 196, 480]})
    assert len(fake.track_calls) == 0
    assert provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']['applied'] is False


def test_h36_incomplete_summary_count_disables_exclusion():
    fake = _DiscoverFake(_make_candidates(16), total=30, truncated=False)
    provider = _provider(fake)
    with pytest.raises(ValueError, match='mask_uncertain_requires_new_prompt:object=block'):
        provider.segment_frame(np.zeros((480, 640, 3), np.uint8), view='right', source=_source(7),
                               exclusion_boxes={'block': [0, 0, 196, 480]})
    assert len(fake.track_calls) == 0
    assert provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']['applied'] is False


def test_h36_retained_or_predicted_fixture_pose_disables_projection_and_held_disables():
    pipe, _ = full_scene()
    try:
        pipe.config['fixture_exclusion_box'] = dict(min=[-0.1, -0.1, -0.2], max=[0.1, 0.1, 0.02])
        cam_pose = np.eye(4)
        fix_pose = np.eye(4)
        fix_pose[:3, 3] = [0.0, 0.0, 0.5]
        retained = FixturePose(
            fixture_id='bucket', pose=tuple(fix_pose.ravel()), reference_pose=tuple(np.eye(4).ravel()),
            frame_id='test-base', source='retained_static_fixture', evidence_ids=('a', 'b'), measured_at_ns=1,
        )
        assert pipe._project_fixture_exclusion_box(retained, cam_pose, pipe.config, 20) is None
        measured = retained.model_copy(update=dict(source='rgbd_anchor_registration'))
        rect = pipe._project_fixture_exclusion_box(measured, cam_pose, pipe.config, 20)
        assert rect is not None and rect[2] > rect[0] and rect[3] > rect[1]
    finally:
        pipe.close()


def test_h36_held_state_never_passes_exclusion_box_in_pipeline(monkeypatch):
    pipe, obs = full_scene()
    try:
        pipe.config['fixture_exclusion_box'] = dict(min=[-0.1, -0.1, -0.2], max=[0.1, 0.1, 0.02])
        fix_pose = np.eye(4); fix_pose[:3, 3] = [0.0, 0.0, 0.5]
        pipe.measured_fixture = FixturePose(
            fixture_id='bucket', pose=tuple(fix_pose.ravel()), reference_pose=tuple(np.eye(4).ravel()),
            frame_id='test-base', source='rgbd_anchor_registration', evidence_ids=('a', 'b'), measured_at_ns=1,
        )
        seen = []
        class _Seg:
            def is_sam_seeded(self, view, key): return False
            def prompt_padding_px(self, view): return 20
            def segment_frame(self, rgb, *, view, source, boxes=None, exclusion_boxes=None):
                seen.append((view, exclusion_boxes))
                raise ValueError('mask_view=' + view + ':mask_uncertain_requires_new_prompt:object=block')
            def diagnostic_snapshot(self): return {}
        pipe.segment = _Seg()
        # Closed gripper -> pregrasp is False (held) -> must NOT pass exclusion_boxes
        cur = sample(obs, 0)
        s16 = list(cur.state16); s16[15] = 85.0
        cur = cur.model_copy(update=dict(state16=tuple(s16)))
        with pytest.raises(ValueError, match='mask_uncertain'):
            pipe.process(cur, base_eef=np.eye(4))
        assert any(v == 'right' and ex is None for v, ex in seen)
    finally:
        pipe.close()


def test_h36_sam_seed_sorts_truncates_64_and_http_header_roundtrips_35(monkeypatch):
    assert SEMANTIC_CANDIDATE_SUMMARY_LIMIT == 64

    # 1) Verify Sam3SeedProvider.seed formatting on 70 synthetic candidates
    class _FakeTensor:
        def __init__(self, arr): self._arr = np.asarray(arr)
        def detach(self): return self
        def float(self): return self
        def cpu(self): return self
        def numpy(self): return self._arr

    class _FakeProc:
        def set_image(self, img): return {}
        def set_text_prompt(self, prompt, state):
            n = 70
            masks = np.zeros((n, 480, 640), dtype=bool)
            boxes = np.zeros((n, 4), dtype=np.float32)
            scores = np.linspace(0.21, 0.98, n, dtype=np.float32)
            for i in range(n):
                masks[i, 10:20, 10:20] = True
                boxes[i] = [10.4 + i, 20.6, 30.2 + i, 40.8]
            return dict(masks=_FakeTensor(masks), boxes=_FakeTensor(boxes), scores=_FakeTensor(scores))

    prov = object.__new__(Sam3SeedProvider)
    prov.identity = 'a' * 64
    prov.prompts = {'square-1': 'wooden block'}
    prov.processor = _FakeProc()
    prov.device = 'cpu'

    import sys
    class _Noop:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    fake_torch = SimpleNamespace(
        inference_mode=lambda: _Noop(),
        autocast=lambda *a, **kw: _Noop(),
        bfloat16='bf16',
    )
    fake_pil_image = SimpleNamespace(fromarray=lambda arr: arr)
    fake_pil = SimpleNamespace(Image=fake_pil_image)
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    monkeypatch.setitem(sys.modules, 'PIL', fake_pil)
    monkeypatch.setitem(sys.modules, 'PIL.Image', fake_pil_image)
    res, detail = prov.seed(np.zeros((480, 640, 3), np.uint8), object_id='square-1', box=[0, 0, 640, 480])
    assert res is None
    assert detail['semantic_candidates'] == 70
    assert detail['semantic_candidate_summary_truncated'] is True
    assert len(detail['semantic_candidate_summary']) == 64
    assert detail['semantic_candidate_summary'][0]['index'] == 69
    assert detail['semantic_candidate_summary'][0]['score'] == 0.98
    assert all(isinstance(v, int) for v in detail['semantic_candidate_summary'][0]['box'])

    # 2) Formal HTTP X-SAM-Tracking-Result roundtrip with 35 candidates
    cands_35 = _make_candidates(35)
    meta_35 = dict(
        stream_id='s1', object_id='square-1', source_ns=12345,
        identity_status='unresolved', identity_source='semantic',
        score_kind='object_presence_not_mask_iou', reseeded=False,
        seed_scope='full_image_semantics', seed_backend='sam3',
        seed_reason='semantic_scope_ambiguous_or_empty',
        semantic_candidates=35, semantic_scoped_candidates=35,
        semantic_selected_index=None, semantic_candidate_summary=cands_35,
        semantic_candidate_summary_truncated=False,
    )
    header_json = json.dumps(meta_35)
    assert len(header_json.encode('utf-8')) < 8192

    model_id = 'm' * 64

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            length = int(self.headers['Content-Length'])
            _ = self.rfile.read(length)
            import io
            buf = io.BytesIO()
            np.savez_compressed(buf, masks=np.zeros((1, 40, 40), bool), scores=[0.0])
            data = buf.getvalue()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-SAM-Image', self.headers['X-SAM-Image'])
            self.send_header('X-SAM-Model', model_id)
            self.send_header('X-SAM-Tracking-Result', header_json)
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        client = RemoteSam2Segmenter(f'http://127.0.0.1:{port}', model_sha256=model_id)
        try:
            _, received = client.discover(np.zeros((40, 40, 3), np.uint8),
                                          stream_id='s1', object_id='square-1', source_ns=12345)
        finally:
            client.close()
    finally:
        server.shutdown()
        server.server_close()
    assert received['semantic_candidates'] == 35
    assert received['semantic_candidate_summary_truncated'] is False
    assert len(received['semantic_candidate_summary']) == 35
    assert received['semantic_candidate_summary'] == cands_35


def _depth_filter_setup():
    # Camera at world [0, 0, 1.0] looking straight down (-Z_world = +Z_cam):
    # p_world = [0, 0, 1.0] + [p_cam_x, -p_cam_y, -p_cam_z]
    cam_world = np.array([
        [1.0, 0.0, 0.0, 0.0],
        [0.0, -1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0, 1.0],
        [0.0, 0.0, 0.0, 1.0],
    ], float)
    k = np.array([[400.0, 0.0, 320.0], [0.0, 400.0, 240.0], [0.0, 0.0, 1.0]], float)
    # Table at z = 0.55 m, normal = [0, 0, 1].
    # Depth 0.40 m -> world z = 0.60 m (5 cm above table -> KEEP)
    # Depth 0.60 m -> world z = 0.40 m (15 cm below table -> EXCLUDE)
    depth = np.full((480, 640), 0.40, float)
    return dict(
        aligned_depth_m=depth,
        k_rgb=k,
        base_camera=cam_world,
        table_point=np.array([0.0, 0.0, 0.55], float),
        table_normal=np.array([0.0, 0.0, 1.0], float),
        below_table_margin_m=0.05,
    )


def test_h45_invalid_depth_candidate_excluded_and_tabletop_kept():
    df = _depth_filter_setup()
    # Candidate 0: on table (center 440, 245, depth 0.40 -> z=0.60 > 0.50)
    # Candidate 1: invalid depth 0.0 at center (389, 38)
    df['aligned_depth_m'][38, 389] = 0.0
    cands = [
        dict(index=0, box=[400, 200, 480, 290], area_px=7000, score=0.97, in_scope=True),
        dict(index=1, box=[377, 27, 401, 49], area_px=268, score=0.26, in_scope=True),
    ]
    fake = _DiscoverFake(cands)
    provider = _provider(fake)
    masks = provider.segment_frame(
        np.zeros((480, 640, 3), np.uint8),
        view='right',
        source=_source(10),
        candidate_depth_filters={'block': df},
    )
    assert masks['block'].sum() >= 100
    diag = provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']
    assert diag['applied'] is True
    assert diag['excluded_by_invalid_depth'] == 1
    assert diag['excluded_by_below_table'] == 0
    assert diag['remaining_candidates'] == 1


def test_h45_below_table_candidate_excluded_and_tabletop_kept():
    df = _depth_filter_setup()
    # Candidate 1: depth 1.15 -> world z = -0.15 m (70 cm below table -> EXCLUDE)
    df['aligned_depth_m'][38, 389] = 1.15
    cands = [
        dict(index=0, box=[400, 200, 480, 290], area_px=7000, score=0.97, in_scope=True),
        dict(index=1, box=[377, 27, 401, 49], area_px=268, score=0.26, in_scope=True),
    ]
    fake = _DiscoverFake(cands)
    provider = _provider(fake)
    masks = provider.segment_frame(
        np.zeros((480, 640, 3), np.uint8),
        view='right',
        source=_source(11),
        candidate_depth_filters={'block': df},
    )
    assert masks['block'].sum() >= 100
    diag = provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']
    assert diag['applied'] is True
    assert diag['excluded_by_below_table'] == 1
    assert diag['excluded_by_invalid_depth'] == 0
    assert diag['remaining_candidates'] == 1


def test_h45_two_tabletop_blocks_both_kept_and_still_unresolved():
    df = _depth_filter_setup()
    # Both candidates have depth 0.40 m -> z = 0.60 m (above table)
    cands = [
        dict(index=0, box=[400, 200, 480, 290], area_px=7000, score=0.97, in_scope=True),
        dict(index=1, box=[200, 200, 280, 290], area_px=6500, score=0.95, in_scope=True),
    ]
    fake = _DiscoverFake(cands)
    provider = _provider(fake)
    with pytest.raises(ValueError, match='mask_uncertain_requires_new_prompt:object=block'):
        provider.segment_frame(
            np.zeros((480, 640, 3), np.uint8),
            view='right',
            source=_source(12),
            candidate_depth_filters={'block': df},
        )
    assert len(fake.track_calls) == 0
    diag = provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']
    assert diag['applied'] is True
    assert diag['excluded_candidates'] == 0
    assert diag['remaining_candidates'] == 2


def test_h45_combined_with_h36_bucket_exclusion():
    df = _depth_filter_setup()
    # Candidate 0: tabletop block; Candidate 1: inside bucket box (also valid depth); Candidate 2: below table
    df['aligned_depth_m'][38, 389] = 1.15
    cands = [
        dict(index=0, box=[400, 200, 480, 290], area_px=7000, score=0.97, in_scope=True),
        dict(index=1, box=[20, 300, 60, 340], area_px=1200, score=0.45, in_scope=True),
        dict(index=2, box=[377, 27, 401, 49], area_px=268, score=0.26, in_scope=True),
    ]
    fake = _DiscoverFake(cands)
    provider = _provider(fake)
    masks = provider.segment_frame(
        np.zeros((480, 640, 3), np.uint8),
        view='right',
        source=_source(13),
        exclusion_boxes={'block': [0, 0, 196, 480]},
        candidate_depth_filters={'block': df},
    )
    assert masks['block'].sum() >= 100
    diag = provider.diagnostic_snapshot()['right']['block']['fixture_exclusion']
    assert diag['excluded_by_fixture'] == 1
    assert diag['excluded_by_below_table'] == 1
    assert diag['remaining_candidates'] == 1
