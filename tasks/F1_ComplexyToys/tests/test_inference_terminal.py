import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('terminal', Path(__file__).resolve().parents[1]/'scripts/inference_terminal.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_ready_requires_fresh_matching_live_descendant(tmp_path):
    path = tmp_path/'status.json'
    d = dict(updated_monotonic_ns=10_000_000_000, pid=9, start_ticks='123',
             status=dict(state='episode_ready'), run_id='test')
    def proc(pid): return {9: (8, '123'), 8: (7, '122'), 7: (1, '121'), 1: (0, '1')}[pid]
    path.write_text(json.dumps(d))
    assert m.ready_status(path, 7, now=11_000_000_000, proc=proc) == d
    assert m.ready_status(path, 5, now=11_000_000_000, proc=proc) is None
    assert m.ready_status(path, 7, now=14_000_000_000, proc=proc) is None
    d['start_ticks']='reused-pid'
    path.write_text(json.dumps(d))
    assert m.ready_status(path, 7, now=11_000_000_000, proc=proc) is None
    d['start_ticks']='123'; d['status']['state']='stopping'
    path.write_text(json.dumps(d))
    assert m.ready_status(path, 7, now=11_000_000_000, proc=proc) is None


def test_rlt_ready_marker_is_outside_renderer_and_no_keys(monkeypatch, capsys):
    calls=[]
    class Result:
        returncode=0
        stdout='7\n'
    monkeypatch.setattr(m.subprocess,'run',lambda args, **kw: (calls.append(args) or Result()))
    monkeypatch.setattr(m,'ready_status',lambda *a: dict(run_id='real-session'))
    monkeypatch.setattr(m.os,'execvp',lambda *a: calls.append(a))
    m.attach('/tmux','f1-only','real-entry',status_path='/status')
    assert m.READY+' real-session' in capsys.readouterr().out
    assert ['/tmux','set-option','-t','f1-only','status','off'] in calls
    assert not any('send-keys' in str(c) or 'kill' in str(c) for c in calls)
    assert calls[-1] == ('/tmux',['/tmux','attach-session','-t','f1-only'])
