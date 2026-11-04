import base64
import importlib.util
import json
from pathlib import Path
import wave
import pytest

spec = importlib.util.spec_from_file_location('doubao_build', Path(__file__).parents[1]/'scripts/76_build_doubao_voice_pack.py')
tts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tts)


def test_chunks_require_success_terminal_and_valid_pcm():
    part = json.dumps({'code': 0, 'data': base64.b64encode(b'\0\1\2\3').decode()}).encode()
    assert tts.decode_audio([part, b'{"code":20000000}']) == b'\0\1\2\3'
    for lines in ([part], [b'{"code":20000000}'], [b'{"code":45000000,"message":"secret"}']):
        with pytest.raises(RuntimeError) as exc:
            tts.decode_audio(lines)
        assert 'secret' not in str(exc.value)


def test_failed_pack_never_replaces_existing_audio(tmp_path):
    def fail(*args): raise RuntimeError('unavailable')
    target = tmp_path/'preview'
    with pytest.raises(RuntimeError): tts.build(target, 'secret', synth=fail)
    assert not target.exists()
    manifest = tts.build(target, 'secret', synth=lambda *args: b'\0\0'*1600)
    assert manifest['runtime_tts'] is False and 'secret' not in json.dumps(manifest)
    with wave.open(str(target/'confirm_rlt.wav')) as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1
    with pytest.raises(FileExistsError): tts.build(target, 'secret', synth=fail)


def test_request_uses_speech_key_and_serialized_lower_pitch():
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self):
            return iter([b'{"code":0,"data":"AAE="}', b'{"code":20000000}'])
    def opener(request, timeout):
        assert request.full_url == tts.ENDPOINT
        assert request.get_header('X-api-key') == 'test-only-key'
        body = json.loads(request.data)
        assert body['req_params']['speaker'] == tts.SPEAKER
        assert json.loads(body['req_params']['additions'])['post_process']['pitch'] == -2
        assert b'test-only-key' not in request.data
        return Response()
    assert tts.synthesize('试听', 'test-only-key', tts.SPEAKER, opener) == b'\0\1'
