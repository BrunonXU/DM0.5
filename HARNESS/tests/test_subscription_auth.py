import json
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from f1_harness.subscription_auth import ISSUER, SignIn, access_token, read_private, write_private


@pytest.fixture
def auth(tmp_path):
    path = tmp_path / 'private' / 'profile.json'
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='test', alg='RS256')
    calls = []
    claims = dict(iss=ISSUER, aud='oaiapp_test', sub='test-subject', iat=time.time(), exp=time.time()+300)
    def handle(req):
        calls.append(req)
        if req.url.path.endswith('jwks.json'):
            return httpx.Response(200, json={'keys': [jwk]})
        tokens = dict(access_token='test-access', refresh_token='test-refresh', token_type='Bearer',
            expires_in=3600, scope='chatgpt.tokens.use.direct',
            id_token=jwt.encode(dict(claims), key, algorithm='RS256', headers={'kid': 'test'}))
        return httpx.Response(200, json=tokens)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    attempt = SignIn(path, 'http://127.0.0.1:1455/auth/callback', client)
    claims['nonce'] = attempt.nonce
    yield attempt, claims, calls
    client.close()


def callback(attempt):
    return dict(state=attempt.state, client_id='oaiapp_test', code='test-code')


def test_signin_verifies_identity_stores_private_and_consumes_callback(auth):
    attempt, _, calls = auth
    query = parse_qs(urlsplit(attempt.authorization_url()).query)
    assert query['client_id'] == ['dynamic_agent_client']
    assert query['code_challenge_method'] == ['S256']
    assert attempt.verifier not in attempt.authorization_url()
    attempt.complete(callback(attempt))
    saved = read_private(attempt.path)
    assert saved['subject'] == 'test-subject' and saved['client_id'] == 'oaiapp_test'
    assert attempt.path.stat().st_mode & 0o777 == 0o600
    assert len(calls) == 2
    with pytest.raises(ValueError):
        attempt.complete(callback(attempt))
    assert len(calls) == 2
    returning = SignIn(attempt.path, attempt.redirect_uri, attempt.client)
    assert returning.host_id == attempt.host_id
    assert 'agent_name_hint' not in parse_qs(urlsplit(returning.authorization_url()).query)


@pytest.mark.parametrize('field,value', [('nonce','wrong'), ('aud','wrong'), ('iss','https://wrong.invalid'),
    ('exp',0), ('sub','')])
def test_invalid_identity_never_saves(auth, field, value):
    attempt, claims, _ = auth
    claims[field] = value
    with pytest.raises((ValueError, jwt.PyJWTError)):
        attempt.complete(callback(attempt))
    assert not attempt.path.exists()


def test_wrong_state_does_not_exchange_and_declined_consent_consumes(auth):
    attempt, _, calls = auth
    with pytest.raises(ValueError, match='state'):
        attempt.complete(dict(callback(attempt), state='wrong'))
    with pytest.raises(ValueError, match='declined'):
        attempt.complete(dict(callback(attempt), error='access_denied'))
    assert not calls and not attempt.path.exists()


def test_refresh_rotates_atomically_and_omits_scope(auth):
    attempt, _, calls = auth
    attempt.complete(callback(attempt))
    saved = read_private(attempt.path)
    write_private(attempt.path, dict(saved, expires_at=0))
    assert access_token(attempt.path, attempt.client) == 'test-access'
    request = parse_qs(calls[-1].content.decode())
    assert request['client_id'] == ['oaiapp_test']
    assert request['grant_type'] == ['refresh_token'] and 'scope' not in request
    count = len(calls)
    access_token(attempt.path, attempt.client)
    assert len(calls) == count


def test_public_credentials_and_symlinks_rejected(auth):
    attempt, _, _ = auth
    attempt.complete(callback(attempt))
    attempt.path.chmod(0o644)
    with pytest.raises(ValueError, match='not_private'):
        read_private(attempt.path)
    link = attempt.path.with_name('link.json')
    link.symlink_to(attempt.path)
    with pytest.raises(OSError):
        read_private(link)
