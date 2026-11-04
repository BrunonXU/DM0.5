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


def test_prompt_wave_has_no_added_silence(tmp_path):
    import array
    dest=tmp_path/'prompt.wav';prepare_wave('resumed',53,dest,lead_s=0,tail_s=0)
    with wave.open(str(dest)) as w:
        assert w.getnframes()==round(.62*16000)
        samples=array.array('h',w.readframes(w.getnframes()))
        assert any(samples[:160])  # Real cue starts in the first 10ms, not after 400ms.


def test_prompt_new_state_discards_pending_old_cues_without_blocking(tmp_path):
    from f1dagger.recording_audio import POLICY_CUE_LEVELS
    class Player:
        def __init__(self):self.token=0;self.entered=threading.Event();self.release=threading.Event();self.calls=[]
        def warmup(self):self.entered.set();assert self.release.wait(3)
        def cancel(self):self.token+=1;return self.token
        def current(self,token):return token==self.token
        def play(self,kind,volume,token):
            if token==self.token:self.calls.append(kind)
    player=Player();a=RecordingAudio(tmp_path/'levels.json',prompt=True,prompt_player=player,cue_levels=POLICY_CUE_LEVELS)
    try:
        assert player.entered.wait(1)
        start=time.monotonic();a.notify('takeover');a.notify('resumed')
        assert time.monotonic()-start<.05
        player.release.set();a.jobs.join()
        assert player.calls==['resumed']
    finally:player.release.set();a.close();a.close()


def test_prompt_interrupt_terminates_only_its_audio_process(tmp_path):
    from f1dagger.recording_audio import PromptPlayer
    entered=threading.Event()
    class Process:
        returncode=None
        def poll(self):return self.returncode
        def terminate(self):self.returncode=-15
        def wait(self,timeout):return self.returncode
    process=Process()
    def spawn(*a,**k):entered.set();return process
    player=PromptPlayer(popen=spawn);token=player.cancel()
    thread=threading.Thread(target=lambda:player.play('takeover',18,token));thread.start()
    assert entered.wait(1);player.cancel();thread.join(1)
    assert not thread.is_alive() and process.returncode==-15


def test_failed_prompt_warmup_still_handles_later_notifications(tmp_path):
    class Player:
        def warmup(self):raise OSError('no sound card')
        def cancel(self):return 1
        def current(self,token):return True
        def play(self,*args):calls.append(args)
    calls=[];errors=[];a=RecordingAudio(tmp_path/'levels.json',prompt=True,prompt_player=Player(),report=errors.append)
    try:
        a.notify('started');a.jobs.join();assert calls and errors
    finally:a.close()



def test_started_cue_queued_after_writer_ready_before_recording_visible(tmp_path):
    snapshots=[]
    class CueOrder(Cues):
        def notify(self,kind):
            snapshots.append((kind,r.phase,r.active is not None,r.begin_ns))
            super().notify(kind)
    cues=CueOrder();r=recorder(tmp_path,audio=cues)
    try:
        r.start(time.monotonic_ns());wait_for(lambda:r.phase=='recording')
        assert snapshots[0][0:3]==('started','starting',True)
        assert snapshots[0][3] is not None
        r.finish(time.monotonic_ns());wait_for(lambda:r.phase=='idle')
        assert r.last_saved['phase']=='saved' and cues.events==['started','saved']
    finally:assert r.close()


def test_failed_open_never_announces_started(tmp_path):
    cues=Cues()
    r=recorder(tmp_path,audio=cues,factory=lambda run:(_ for _ in ()).throw(OSError('disk unavailable')))
    r.start(time.monotonic_ns());wait_for(lambda:r.phase=='failed')
    assert cues.events==[] and not r.close()


def test_audio_admission_failure_does_not_fail_recording_or_commit(tmp_path):
    class BrokenAudio:
        def notify(self,kind):raise OSError('audio unavailable')
        def close(self):raise OSError('audio teardown unavailable')
    r=recorder(tmp_path,audio=BrokenAudio())
    try:
        r.start(time.monotonic_ns());wait_for(lambda:r.phase=='recording')
        assert r.writer_error is None and 'audio unavailable' in r.diagnostics()['audio_error']
        r.finish(time.monotonic_ns());wait_for(lambda:r.phase=='idle')
        assert r.last_saved['phase']=='saved' and r.error is None
    finally:assert r.close()


def test_success_ack_is_not_cancelled_by_immediate_auto_return(tmp_path):
    from f1dagger.recording_audio import POLICY_CUE_LEVELS
    class Player:
        def __init__(self):self.token=0;self.entered=threading.Event();self.release=threading.Event();self.calls=[]
        def warmup(self):self.entered.set();assert self.release.wait(2)
        def cancel(self):self.token+=1;return self.token
        def current(self,token):return token==self.token
        def play(self,kind,volume,token):
            if self.current(token):self.calls.append(kind)
    player=Player();a=RecordingAudio(tmp_path/'levels.json',prompt=True,prompt_player=player,cue_levels=POLICY_CUE_LEVELS)
    try:
        assert player.entered.wait(1);a.notify('local_success');a.notify('resumed')
        player.release.set();a.jobs.join();assert player.calls==['local_success']
        a.notify('resumed');a.jobs.join();assert player.calls==['local_success','resumed']
    finally:player.release.set();a.close()


def test_early_start_supersedes_warmup_silence():
    from f1dagger.recording_audio import PromptPlayer
    calls=[];player=PromptPlayer(popen=lambda *a,**k:calls.append(a))
    player.cancel();player.warmup();assert not calls
