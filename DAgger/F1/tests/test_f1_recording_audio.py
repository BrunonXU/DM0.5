import threading
import wave
import time
from f1dagger.recording_audio import RecordingAudio, levels, save_levels, prepare_wave
from test_f1_demonstration import recorder, wait_for

class Cues:
    def __init__(self):self.events=[]
    def notify(self,kind):self.events.append(kind)
    def close(self):pass

def test_two_episodes_only_signal_real_transitions(tmp_path):
    cues=Cues();r=recorder(tmp_path,audio=cues)
    try:
        assert cues.events==[]
        for _ in range(2):
            r.start(time.monotonic_ns());wait_for(lambda:r.phase=='recording')
            assert cues.events[-1]=='started'
            r.finish(time.monotonic_ns());wait_for(lambda:r.phase=='idle')
            assert r.last_saved['phase']=='saved' and cues.events[-1]=='saved'
        assert cues.events==['started','saved','started','saved']
    finally:r.close()

def test_failed_close_does_not_signal_success(tmp_path):
    cues=Cues();r=recorder(tmp_path,audio=cues)
    try:
        r.start(time.monotonic_ns());wait_for(lambda:r.phase=='recording')
        r.finish(time.monotonic_ns(),status='failed');wait_for(lambda:r.phase=='failed')
        assert cues.events==['started']
    finally:r.close()

def test_slow_or_failed_audio_cannot_block_notifications(tmp_path):
    entered=threading.Event();release=threading.Event();errors=[]
    def slow(*_):entered.set();release.wait(3);raise OSError('no audio device')
    a=RecordingAudio(tmp_path/'levels.json',player=slow,report=errors.append)
    try:
        a.notify('started');assert entered.wait(1)
        for _ in range(20):a.notify('saved')
        assert a.jobs.qsize()<=4
    finally:release.set();a.close()
    assert errors and a.closed

def test_gain_config_and_wav(tmp_path):
    path=tmp_path/'levels.json'
    save_levels(path,{'started':27,'saved':41})
    assert levels(path)=={'started':27,'saved':41}
    dest=tmp_path/'cue.wav';prepare_wave('started',0,dest)
    with wave.open(str(dest)) as w:
        assert w.getframerate()==16000 and w.getnchannels()==1
        assert not any(w.readframes(w.getnframes()))
    path.write_text('{broken');assert levels(path)=={'started':18,'saved':18}


def test_three_commits_read_saved_levels_without_restarting_audio(tmp_path):
    path=tmp_path/'levels.json'
    save_levels(path,{'started':53,'saved':50})
    calls=[]
    audio=RecordingAudio(path,player=lambda kind,volume:calls.append((kind,volume)))
    r=recorder(tmp_path,audio=audio)
    ids=[]
    try:
        for i in range(3):
            assert r.start(time.monotonic_ns())
            wait_for(lambda:r.phase=='recording')
            wait_for(lambda:len(calls)==2*i+1)
            ids.append(r.run_id)
            r.finish(time.monotonic_ns())
            wait_for(lambda:r.phase=='idle')
            wait_for(lambda:len(calls)==2*i+2)
            assert r.last_saved['phase']=='saved'
            if i==0:
                save_levels(path,{'started':37,'saved':29})
        assert len(set(ids))==3
        assert calls==[('started',53),('saved',50)]+[('started',37),('saved',29)]*2
    finally:assert r.close()


def test_blocked_audio_does_not_block_save_or_next_episode(tmp_path):
    entered=threading.Event();release=threading.Event();errors=[]
    def unavailable(*_):
        entered.set()
        assert release.wait(10)
        raise OSError('audio unavailable')
    audio=RecordingAudio(tmp_path/'levels.json',player=unavailable,report=errors.append)
    r=recorder(tmp_path,audio=audio)
    try:
        assert r.start(time.monotonic_ns())
        wait_for(lambda:r.phase=='recording')
        assert entered.wait(1)
        first=r.run_id
        r.finish(time.monotonic_ns())
        wait_for(lambda:r.phase=='idle')
        assert r.last_saved['phase']=='saved' and not release.is_set()
        assert r.start(time.monotonic_ns())
        wait_for(lambda:r.phase=='recording')
        assert r.run_id!=first
        r.finish(time.monotonic_ns())
        wait_for(lambda:r.phase=='idle')
        assert r.last_saved['phase']=='saved' and not release.is_set()
    finally:release.set();assert r.close()
    assert errors
