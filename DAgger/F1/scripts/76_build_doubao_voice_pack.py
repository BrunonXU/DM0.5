"""Build an offline F1 Mandarin prompt pack using Doubao TTS 2.0.

Credentials stay on the build host. Output is a NEW directory for listening
and release packaging; this tool never changes a running robot's audio.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.request
import uuid
import wave

ENDPOINT = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional'
SPEAKER = 'zh_female_sophie_uranus_bigtts'
PITCH = -2
PHRASES = {
    'confirm_rlt': '请评分。',
    'communication_wait': '连接中断。',
    'motion_limited': '动作受限。',
    'system_fault': '系统故障。',
}


def decode_audio(lines):
    chunks = []
    complete = False
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            code = event['code']
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('Invalid TTS response') from None
        if code not in (0, 20000000):
            # Never dump server messages, headers or request objects into logs.
            raise RuntimeError('TTS rejected request; code=' + str(code))
        if event.get('data'):
            try:
                chunks.append(base64.b64decode(event['data'], validate=True))
            except (ValueError, TypeError):
                raise RuntimeError('Invalid TTS audio encoding') from None
        if code == 20000000:
            complete = True
            break
    pcm = b''.join(chunks)
    if not complete or not pcm or len(pcm) % 2:
        raise RuntimeError('Incomplete or invalid 16-bit TTS audio')
    return pcm


def synthesize(text, key, speaker, opener=urllib.request.urlopen):
    payload = {'user': {'uid': 'f1-offline-prompts'}, 'req_params': {
        'text': text, 'speaker': speaker,
        'audio_params': {'format': 'pcm', 'sample_rate': 16000},
        'additions': json.dumps({'post_process': {'pitch': PITCH}})}}
    request = urllib.request.Request(ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'X-Api-Key': key,
                 'X-Api-Resource-Id': 'seed-tts-2.0', 'X-Api-Request-Id': str(uuid.uuid4())})
    try:
        with opener(request, timeout=60) as response:
            return decode_audio(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'TTS HTTP {exc.code}; check speech key, resource and voice permissions') from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('TTS connection failed; no voice pack published') from None


def build(destination, key, speaker=SPEAKER, synth=synthesize):
    destination = Path(destination)
    if not key or not key.strip():
        raise ValueError('Set DOUBAO_SPEECH_API_KEY or provide --api-key-file on the build host')
    if destination.exists():
        raise FileExistsError('Use a new preview directory; existing voice packs are immutable')
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = dict(schema='f1_voice_pack_v1', engine='doubao/seed-tts-2.0',
                    speaker=speaker, pitch=PITCH, runtime_tts=False, phrases={})
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.voice-build-') as tmp:
        staging = Path(tmp) / 'pack'
        staging.mkdir()
        for name, phrase in PHRASES.items():
            pcm = synth(phrase, key.strip(), speaker)
            if not pcm or len(pcm) % 2:
                raise RuntimeError('Invalid synthesized PCM')
            target = staging / (name + '.wav')
            with wave.open(str(target), 'wb') as out:
                out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                out.writeframes(pcm)
            manifest['phrases'][name] = dict(text=phrase, duration_s=len(pcm)/32000,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        (staging/'voice-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        staging.rename(destination)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--api-key-file', type=Path)
    parser.add_argument('--speaker', default=SPEAKER)
    args = parser.parse_args()
    key = args.api_key_file.read_text().strip() if args.api_key_file else os.environ.get('DOUBAO_SPEECH_API_KEY', '')
    try:
        build(args.output, key, args.speaker)
    except (ValueError, RuntimeError, FileExistsError) as exc:
        parser.exit(1, str(exc)+'\n')
    print('Voice preview ready: '+str(args.output))
