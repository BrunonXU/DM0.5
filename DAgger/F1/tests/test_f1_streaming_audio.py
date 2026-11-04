import io
import json
import time
import threading
from f1dagger import recording_audio as audio


class Process:
    def __init__(self):self.stdin=io.BytesIO();self.returncode=None
    def poll(self):return self.returncode
    def terminate(self):self.returncode=0
    def wait(self,timeout=None):return self.returncode
    def kill(self):self.returncode=-9


def test_device_stays_open_and_original_pcm_replaces_silence():
    calls=[];reports=[];process=Process()
    def spawn(*a,**kw):calls.append(a);return process
    player=audio.StreamingPromptPlayer(popen=spawn,report=reports.append)
    try:
        player.warmup();time.sleep(.03)
        assert not any(process.stdin.getvalue())
        player.play('started',100,player.cancel())
        player.play('takeover',100,player.cancel())
        assert len(calls)==1 and process.poll() is None
        data=process.stdin.getvalue()
        assert player.assets['started'] in data and player.assets['takeover'] in data
        for report in reports:
            row=json.loads(report.removeprefix('F1 AUDIO '))
            assert 0<=row['pcm_submit_monotonic_ns']-row['queued_monotonic_ns']<200_000_000
    finally:player.close()
    assert process.poll()==0 and not player.thread.is_alive()


def test_cancel_discards_unplayed_tail_without_reopening_device():
    process=Process();player=audio.StreamingPromptPlayer(popen=lambda *a,**kw:process,report=lambda *_:None)
    player.warmup();token=player.cancel()
    thread=threading.Thread(target=player.play,args=('started',100,token));thread.start()
    time.sleep(.03);player.cancel();thread.join(1)
    try:
        assert not thread.is_alive() and process.poll() is None
        time.sleep(.04);before=len(process.stdin.getvalue());time.sleep(.04)
        assert not any(process.stdin.getvalue()[before:])
    finally:player.close()


def test_missing_assets_are_optional_audio_failure(tmp_path,monkeypatch):
    monkeypatch.setattr(audio,'ASSETS',tmp_path)
    reports=[]
    player=audio.RecordingAudio(tmp_path/'config',prompt=True,report=reports.append)
    player.notify('started');player.jobs.join();player.close()
    assert any('unavailable' in row for row in reports)
