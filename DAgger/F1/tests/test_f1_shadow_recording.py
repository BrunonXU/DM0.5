import json
from pathlib import Path
import pytest
from f1dagger.shadow_recording import ShadowRecorder, workspace
from f1dagger.recording import F1Recorder, make_sink


def test_shadow_discards_payloads_and_keeps_bounded_counters(tmp_path):
    recorder=ShadowRecorder();sink=make_sink(tmp_path/'payload','inference-only');sentinel=object()
    for _ in range(10000):
        assert recorder.record_timestamped_observation(sentinel)
        assert recorder.record_chunk(sentinel)
        assert recorder.record_event('event',anything=sentinel)
        assert recorder.control(submitted=None,published=False,selected=sentinel)
        assert sink.submit(sentinel)
    assert recorder.summary()==dict(recording=False,counts=dict(observations=10000,policy_chunks=10000,events=10000,control=10000),submitted=0,published=0)
    assert list(tmp_path.iterdir())==[]
    assert all(v is not sentinel for v in vars(recorder).values())
    assert recorder.close() and sink.close()
    assert not recorder.record_event('after close')


@pytest.mark.parametrize('failed',[False,True])
def test_temporary_workspace_removed_on_normal_and_error_exit(tmp_path,failed):
    temp=workspace(tmp_path/'runtime');path=Path(temp.name)
    try:
        with temp:
            (path/'model_runtime').mkdir();(path/'model_runtime/robot.urdf').write_text('asset')
            if failed:raise RuntimeError('failure')
    except RuntimeError:assert failed
    assert not path.exists() and not (tmp_path/'raw').exists()


def test_powered_recorder_still_commits_journal(tmp_path):
    r=F1Recorder(tmp_path/'journal',episode_id='real',task_prompt='task',binding={'execution_mode':'powered'})
    assert r.record_event('operator_authorized',monotonic_ns=123) and r.close()
    m=json.loads((tmp_path/'journal/manifest.json').read_text())
    assert m['complete'] and m['counts']['events']==1 and m['binding']['execution_mode']=='powered'
