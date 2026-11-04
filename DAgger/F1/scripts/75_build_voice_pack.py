"""Build short Mandarin WAV assets offline using the installed eSpeak NG library.

Only build-time synthesis; the robot plays packaged PCM, never calls TTS.
"""
import ctypes
import ctypes.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import wave

PHRASES = {
    'confirm_rlt': 'R L T，确认按钮',
    'need_takeover': '请按 START 接管',
    'manual_recovery': '松 R B 回中，再按 R B',
    'protection_locked': '保护锁定，请看屏幕',
    'recording_failed': '录制失败，请停止采集',
    'communication_wait': '通信等待',
}


def build(destination):
    library = ctypes.util.find_library('espeak-ng')
    if not library:
        raise RuntimeError('Build host needs eSpeak NG; robot does not')
    engine = ctypes.CDLL(library)
    rate = engine.espeak_Initialize(2, 0, None, 0)
    if rate <= 0 or engine.espeak_SetVoiceByName(b'cmn') != 0:
        raise RuntimeError('Mandarin synthesis unavailable')
    engine.espeak_SetParameter(1, 185, 0)
    buffers = []
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
    @callback_type
    def receive(samples, count, events):
        if samples and count:
            buffers.append(ctypes.string_at(samples, count*2))
        return 0
    engine.espeak_SetSynthCallback(receive)
    destination.mkdir(parents=True, exist_ok=True)
    manifest = dict(schema='f1_voice_pack_v1', engine='espeak-ng/cmn', runtime_tts=False, phrases={})
    with tempfile.TemporaryDirectory(prefix='f1-voice-') as tmp:
        for name, phrase in PHRASES.items():
            buffers.clear()
            text = phrase.encode('utf-8')+b'\0'
            if engine.espeak_Synth(text, len(text), 0, 1, 0, 1, None, None) != 0:
                raise RuntimeError('Synthesis failed: '+name)
            engine.espeak_Synchronize()
            source = Path(tmp)/(name+'.wav')
            with wave.open(str(source), 'wb') as output:
                output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                output.writeframes(b''.join(buffers))
            target = destination/(name+'.wav')
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(source),'-ar','16000','-ac','1',str(target)],check=True)
            with wave.open(str(target)) as output:
                duration = output.getnframes()/output.getframerate()
            manifest['phrases'][name] = dict(text=phrase, duration_s=duration,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    (destination/'voice-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    engine.espeak_Terminate()
    return manifest


if __name__ == '__main__':
    print(json.dumps(build(Path(__file__).resolve().parents[1]/'src/f1dagger/audio'),ensure_ascii=False))
