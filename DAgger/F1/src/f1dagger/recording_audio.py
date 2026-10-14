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


def prepare_wave(kind, volume, destination):
    if kind not in DEFAULTS:
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
        target.writeframes(bytes(round(rate*.4)*2)+adjusted.tobytes()+bytes(round(rate*.15)*2))


def play(kind, volume):
    if volume <= 0:
        return
    with tempfile.TemporaryDirectory(prefix='f1-recording-audio-') as tmp:
        path = Path(tmp)/'cue.wav'
        prepare_wave(kind, volume, path)
        subprocess.run(['aplay', '-q', str(path)], check=True, timeout=4,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)


class RecordingAudio:
    def __init__(self, config=DEFAULT_CONFIG, *, player=play, report=print):
        self.config, self.player, self.report = Path(config), player, report
        self.jobs = queue.Queue(maxsize=4)
        self.closed = False
        self.worker = threading.Thread(target=self._work, name='f1-recording-audio', daemon=True)
        self.worker.start()

    def notify(self, kind):
        if self.closed or kind not in DEFAULTS:
            return
        try:
            self.jobs.put_nowait((kind, time.monotonic()))
        except queue.Full:
            pass  # Never block a recording transition to play a sound.

    def _work(self):
        while True:
            item = self.jobs.get()
            try:
                if item is None:
                    return
                kind, created = item
                if time.monotonic()-created <= 2:
                    self.player(kind, levels(self.config)[kind])
            except Exception as exc:
                self.report('F1 AUDIO unavailable: '+str(exc))
            finally:
                self.jobs.task_done()

    def close(self):
        self.closed = True
        try:
            self.jobs.put(None, timeout=1)
        except queue.Full:
            return
        self.worker.join(5)
