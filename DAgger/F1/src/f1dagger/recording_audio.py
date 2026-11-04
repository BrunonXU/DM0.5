"""Optional recording cues: bounded worker, software gain, no control dependencies."""
import array
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave

ASSETS = Path(__file__).parent/'audio'
DEFAULT_CONFIG = Path.home()/'Bruno/DAgger-runtime/f1-09/recording_audio.json'
DEFAULTS = {'started': 18, 'saved': 18}
VOICE_FILES = frozenset(('confirm_rlt', 'communication_wait', 'motion_limited', 'system_fault'))
CUE_FILES = frozenset(('started', 'saved', 'takeover', 'resumed', 'rlt_started', 'local_success')) | VOICE_FILES
POLICY_CUE_LEVELS = {k:('saved' if k in ('saved','local_success') else 'started') for k in CUE_FILES}


def levels(path):
    try:
        value = json.loads(Path(path).read_text())
        return {k: max(0, min(100, int(value[k]))) for k in DEFAULTS}
    except (OSError, ValueError, TypeError, KeyError):
        return dict(DEFAULTS)


def save_levels(path, value):
    value = {k: max(0, min(100, int(value[k]))) for k in DEFAULTS}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2)+'\n')
    tmp.replace(path)


def prepare_wave(kind, volume, destination, *, lead_s=.4, tail_s=.15):
    if kind not in CUE_FILES:
        raise ValueError('unknown recording cue')
    with wave.open(str(ASSETS/(kind+'.wav')), 'rb') as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError('cue must be mono PCM16')
        rate = source.getframerate()
        samples = array.array('h', source.readframes(source.getnframes()))
    if sys.byteorder != 'little':
        samples.byteswap()
    gain = max(0, min(100, volume))/100
    adjusted = array.array('h', (round(x*gain) for x in samples))
    if sys.byteorder != 'little':
        adjusted.byteswap()
    with wave.open(str(destination), 'wb') as target:
        target.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        # Give a sleeping audio path time to open before the short audible cue.
        # This does not delay recording: only the audio worker waits.
        target.writeframes(bytes(round(rate*lead_s)*2)+adjusted.tobytes()+bytes(round(rate*tail_s)*2))


def play(kind, volume):
    if volume <= 0:
        return
    with tempfile.TemporaryDirectory(prefix='f1-recording-audio-') as tmp:
        path = Path(tmp)/'cue.wav'
        prepare_wave(kind, volume, path)
        subprocess.run(['aplay', '-q', str(path)], check=True, timeout=4,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)


class PromptPlayer:
    """One audio worker owns aplay; control only revokes obsolete cue tokens."""
    def __init__(self, *, popen=subprocess.Popen):
        self.popen = popen
        self.generation = 0
        self.lock = threading.Lock()

    def cancel(self):
        with self.lock:
            self.generation += 1
            return self.generation

    def current(self, token):
        with self.lock:
            return token == self.generation

    def _play_file(self, path, token):
        if not self.current(token):
            return
        process = self.popen(['aplay', '-q', str(path)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic()+4.
        try:
            while process.poll() is None:
                if not self.current(token):
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError('audio playback timeout')
                time.sleep(.01)
            if process.returncode:
                raise RuntimeError('audio playback exited: '+str(process.returncode))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=.2)

    def warmup(self):
        # This runs once in the audio thread during startup, before real START.
        token = 0  # Any already accepted cue supersedes startup silence.
        with tempfile.TemporaryDirectory(prefix='f1-audio-warmup-') as tmp:
            path = Path(tmp)/'silence.wav'
            with wave.open(str(path), 'wb') as target:
                target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                target.writeframes(bytes(12800))
            self._play_file(path, token)

    def play(self, kind, volume, token):
        if volume <= 0 or not self.current(token):
            return
        with tempfile.TemporaryDirectory(prefix='f1-prompt-audio-') as tmp:
            path = Path(tmp)/'cue.wav'
            prepare_wave(kind, volume, path, lead_s=0., tail_s=0.)
            self._play_file(path, token)


class StreamingPromptPlayer(PromptPlayer):
    """Keep the audio device open; a cue replaces silence in the next 10ms block.

    Only this worker touches aplay/PCM. The control loop never waits for audio.
    Existing PCM assets and live software volume stay unchanged.
    """
    def __init__(self, *, popen=subprocess.Popen, report=print):
        super().__init__(popen=popen)
        self.report = report
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.stream_lock = threading.Lock()
        self.active = None
        self.process = None
        self.thread = None
        self.error = None
        self.assets = {}

    def warmup(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._stream,name='f1-audio-pcm',daemon=True)
            self.thread.start()
        if not self.ready.wait(2) or self.error:
            raise RuntimeError('Prompt stream unavailable: '+str(self.error))

    def _stream(self):
        try:
            for kind in CUE_FILES:
                with wave.open(str(ASSETS/(kind+'.wav')),'rb') as source:
                    if (source.getnchannels(),source.getsampwidth(),source.getframerate()) != (1,2,16000):
                        raise ValueError('Prompt stream requires mono PCM16 16kHz')
                    self.assets[kind] = source.readframes(source.getnframes())
            self.process = self.popen(['aplay','-q','-t','raw','-f','S16_LE','-r','16000','-c','1',
                '--buffer-time=40000','--period-time=10000'],stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,bufsize=0)
            deadline = time.monotonic()
            while not self.stop.is_set():
                if self.process.poll() is not None:
                    raise RuntimeError('aplay stream exited: '+str(self.process.returncode))
                onset = None
                with self.stream_lock:
                    active = self.active
                    if active is not None and not self.current(active['token']):
                        active['done'].set();self.active=None;active=None
                    block = bytes(320)
                    if active is not None:
                        offset = active['offset'];block=active['data'][offset:offset+320].ljust(320,b'\0')
                        active['offset'] += 320
                        if offset == 0:
                            onset = dict(kind=active['kind'],token=active['token'],
                                queued_monotonic_ns=active['queued'],pcm_submit_monotonic_ns=time.monotonic_ns())
                self.process.stdin.write(block)
                self.ready.set()
                if onset:
                    active['onset']=dict(onset,pcm_submit_monotonic_ns=time.monotonic_ns())
                with self.stream_lock:
                    if active is not None and active['offset'] >= len(active['data']):
                        active['done'].set()
                        if self.active is active:self.active=None
                deadline = max(deadline+.01,time.monotonic())
                self.stop.wait(max(0.,deadline-time.monotonic()))
        except Exception as exc:
            self.error=type(exc).__name__+': '+str(exc)
            self.ready.set()
        finally:
            with self.stream_lock:
                if self.active is not None:self.active['done'].set()

    def play(self,kind,volume,token):
        if volume<=0 or not self.current(token):return
        self.warmup()
        samples=array.array('h',self.assets[kind])
        if sys.byteorder!='little':samples.byteswap()
        samples=array.array('h',(round(v*volume/100) for v in samples))
        if sys.byteorder!='little':samples.byteswap()
        done=threading.Event()
        job=dict(kind=kind,token=token,data=samples.tobytes(),offset=0,done=done,queued=time.monotonic_ns())
        with self.stream_lock:
            self.active=job
        until=time.monotonic()+4
        while not done.wait(.02):
            if not self.current(token) or self.stop.is_set():return
            if time.monotonic()>until:raise TimeoutError('Prompt stream stalled')
        if self.error:raise RuntimeError(self.error)
        if job.get('onset'):
            self.report('F1 AUDIO '+json.dumps(dict(event='pcm_submitted',**job['onset'])))

    def close(self):
        self.cancel();self.stop.set()
        process=self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=.5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=.5)
        if self.thread is not None:self.thread.join(timeout=1)
        if process is not None and process.stdin is not None:
            process.stdin.close()


class RecordingAudio:
    def __init__(self, config=DEFAULT_CONFIG, *, player=play, report=print, cue_levels=None, prompt=False, prompt_player=None):
        self.config, self.player, self.report = Path(config), player, report
        self.prompt_player = (prompt_player or StreamingPromptPlayer(report=report)) if prompt else None
        self.cue_levels = dict(cue_levels if cue_levels is not None else {k:k for k in DEFAULTS})
        if any(k not in CUE_FILES or v not in DEFAULTS for k,v in self.cue_levels.items()):
            raise ValueError('unknown cue or volume setting')
        self.notification_lock = threading.Lock()
        self._prompt_kind = None
        self.jobs = queue.Queue(maxsize=4)
        self.closed = False
        self.worker = threading.Thread(target=self._work, name='f1-recording-audio', daemon=True)
        self.worker.start()

    def notify(self, kind):
        with self.notification_lock:
            if self.closed or kind not in self.cue_levels:
                return
            token = None
            if self.prompt_player is not None:
                # Success may automatically return to VLA in the same tick.
                # Keep that acknowledgement audible; text still reports VLA.
                if kind == 'resumed' and self._prompt_kind in ('local_success', 'confirm_rlt'):
                    return
                token = self.prompt_player.cancel()
                self._prompt_kind = kind
                # New accepted state supersedes pending sounds of the old state.
                while True:
                    try:
                        self.jobs.get_nowait(); self.jobs.task_done()
                    except queue.Empty:
                        break
            try:
                self.jobs.put_nowait((kind, time.monotonic(), token))
            except queue.Full:
                pass  # Never block a recording transition to play a sound.

    def cancel_exception(self):
        with self.notification_lock:
            if self.prompt_player is not None and self._prompt_kind in VOICE_FILES:
                self.prompt_player.cancel()
                self._prompt_kind = None

    def _work(self):
        if self.prompt_player is not None:
            try:
                self.prompt_player.warmup()
            except Exception as exc:
                self.report('F1 AUDIO warmup unavailable: '+str(exc))
        while True:
            item = self.jobs.get()
            token = None
            try:
                if item is None:
                    return
                kind, created, token = item
                if time.monotonic()-created <= 2:
                    volume = levels(self.config)[self.cue_levels[kind]]
                    if self.prompt_player is None:
                        self.player(kind, volume)
                    else:
                        self.prompt_player.play(kind, volume, token)
            except Exception as exc:
                self.report('F1 AUDIO unavailable: '+str(exc))
            finally:
                if self.prompt_player is not None and token is not None:
                    with self.notification_lock:
                        if self.prompt_player.current(token):
                            self._prompt_kind = None
                self.jobs.task_done()

    def close(self):
        with self.notification_lock:
            if self.closed:
                return
            self.closed = True
            if self.prompt_player is not None:
                self.prompt_player.cancel()
            try:
                self.jobs.put(None, timeout=1)
            except queue.Full:
                return
        self.worker.join(5)
        if self.prompt_player is not None and hasattr(self.prompt_player,'close'):
            self.prompt_player.close()
