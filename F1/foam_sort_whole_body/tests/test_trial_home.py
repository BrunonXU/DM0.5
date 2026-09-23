from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay import trial_home as home
from f1_replay.runtime import ReplayError


def test_rejects_old_robot_and_reboot_even_if_host_is_same():
    with pytest.raises(ReplayError):home.validate_session({'machine_id':'old','boot_id':'boot'},'new','boot')
    with pytest.raises(ReplayError):home.validate_session({'machine_id':'new','boot_id':'old'},'new','new')


def test_pose_and_anchor_cannot_silently_change(tmp_path,monkeypatch):
    monkeypatch.setattr(home,'ROOT',tmp_path)
    d=tmp_path/'runtime/base_home/4F-F1/session';d.mkdir(parents=True)
    for name in ('pose.json','anchor.json','restore.json'):(d/name).write_text('{}')
    index=dict(machine_id='m',boot_id='b',directory=str(d),files={n.name:home.sha(n) for n in d.iterdir()})
    assert home.validate_session(index,'m','b') == d
    assert home.validate_session(index,'m','new_boot',False) == d
    with pytest.raises(ReplayError):home.validate_session(index,'other','new_boot',False)
    (d/'anchor.json').write_text('{"changed":true}')
    with pytest.raises(ReplayError,match='哈希'):home.validate_session(index,'m','b')
    with pytest.raises(ReplayError,match='哈希'):home.validate_session(index,'m','new_boot',False)


def test_restore_pose_after_reboot_does_not_use_old_base_coordinates(tmp_path,monkeypatch):
    import json,socket
    monkeypatch.setattr(home,'ROOT',tmp_path)
    d=tmp_path/'runtime/base_home/4F-F1/session';d.mkdir(parents=True)
    (d.parent/'latest.json').write_text('{}')
    # No anchor is read and no base command path is entered for pose-only restore.
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'expected_machine_id':Path('/etc/machine-id').read_text().strip(),
                                  'expected_host':socket.gethostname()}))
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'profile':str(profile),'restore_arms_only':True}))
    def validate(index,machine,boot,require_same_boot):
        assert require_same_boot is False
        return d
    monkeypatch.setattr(home,'validate_session',validate)
    monkeypatch.setattr(home.base_home,'main',lambda *a:pytest.fail('base path entered'))
    monkeypatch.setattr(home.base_home,'validate_anchor',lambda *a:pytest.fail('odometry used'))
    calls=[]
    monkeypatch.setattr(home.pose_cli,'main',lambda args:calls.append(args) or 0)
    assert home.main(['restore-pose','--config',str(config)])==0
    assert calls==[['restore','--config',str(d/'restore.json'),'--arms-only']]


@pytest.mark.parametrize('base_result', [0, 2])
def test_one_restore_includes_arms_only_after_successful_base_return(tmp_path,monkeypatch,base_result):
    import json,socket
    monkeypatch.setattr(home,'ROOT',tmp_path)
    d=tmp_path/'runtime/base_home/4F-F1/session';d.mkdir(parents=True)
    (d.parent/'latest.json').write_text('{}');(d/'anchor.json').write_text('{}')
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'expected_machine_id':Path('/etc/machine-id').read_text().strip(),
                                   'expected_host':socket.gethostname()}))
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'profile':str(profile),'align_yaw_on_return':True,'restore_arms_only':True}))
    monkeypatch.setattr(home,'validate_session',lambda *a:d)
    monkeypatch.setattr(home.base_home,'validate_anchor',lambda *a:None)
    calls=[]
    def base(args):calls.append(('base',args));return base_result
    def pose(args):calls.append(('pose',args));return 0
    monkeypatch.setattr(home.base_home,'main',base);monkeypatch.setattr(home.pose_cli,'main',pose)
    assert home.main(['restore','--config',str(config)])==base_result
    assert calls[0][1][0]=='return' and '--align-yaw' in calls[0][1]
    if base_result==0:
        assert calls[1][1][0]=='restore' and '--arms-only' in calls[1][1]
    else:assert len(calls)==1
