"""Official SIWC public-client authentication. Tokens never cross planner IPC."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from urllib.parse import urlencode
import uuid

import httpx
import jwt

ISSUER = 'https://auth.openai.com'
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
SCOPE = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('subscription_directory_not_private')
    return path


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('subscription_file_not_private')
        return json.load(stream)


def write_private(path, value):
    path = Path(path)
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.credential-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def credential_lock(path):
    path = Path(path)
    private_directory(path.parent)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def token_record(tokens, previous):
    scopes = tokens.get('scope', ' '.join(previous.get('scopes', []))).split()
    if 'chatgpt.tokens.use.direct' not in scopes:
        raise ValueError('subscription_plan_permission_missing')
    if (tokens.get('token_type', '').lower() != 'bearer' or
            not isinstance(tokens.get('access_token'), str) or not tokens['access_token']):
        raise ValueError('subscription_token_response_invalid')
    expires = float(tokens['expires_in'])
    if not 0 < expires < 366 * 86400:
        raise ValueError('subscription_expiry_invalid')
    value = dict(previous, access_token=tokens['access_token'], scopes=scopes,
                 expires_at=time.time() + expires, token_type='Bearer')
    for name in ('refresh_token', 'id_token'):
        if tokens.get(name):
            value[name] = tokens[name]
    if not value.get('refresh_token'):
        raise ValueError('subscription_refresh_token_missing')
    return value


def access_token(path, client):
    """Serialize rotating refreshes across the pool and independent preflight."""
    with credential_lock(path):
        value = read_private(path)
        if 'chatgpt.tokens.use.direct' not in value.get('scopes', []):
            raise ValueError('subscription_plan_permission_missing')
        if value['expires_at'] <= time.time() + 60:
            response = client.post(TOKEN_URL, data=dict(grant_type='refresh_token',
                client_id=value['client_id'], refresh_token=value['refresh_token'], resource=RESOURCE))
            if response.status_code != 200:
                raise ValueError('subscription_refresh_failed_reauthorize')
            value = token_record(response.json(), value)
            write_private(path, value)
        return value['access_token']


class SignIn:
    def __init__(self, path, redirect_uri, client):
        self.path, self.redirect_uri, self.client = Path(path), redirect_uri, client
        private_directory(self.path.parent)
        host_path = self.path.parent / 'host.json'
        with credential_lock(host_path):
            if not host_path.exists():
                write_private(host_path, {'id': 'urn:uuid:' + str(uuid.uuid4())})
            self.host_id = read_private(host_path)['id']
        self.previous = read_private(self.path) if self.path.exists() else {}
        self.state, self.nonce, self.verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self.deadline = time.monotonic() + 1800
        self.consumed = False

    def authorization_url(self):
        params = dict(client_id=self.previous.get('client_id', 'dynamic_agent_client'),
            ext_agent_host_id=self.host_id, response_type='code', redirect_uri=self.redirect_uri,
            scope=SCOPE, resource=RESOURCE, state=self.state, nonce=self.nonce,
            code_challenge_method='S256', code_challenge=base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('='))
        if not self.previous.get('client_id'):
            params['agent_name_hint'] = 'F1 Harness'
        elif self.previous.get('id_token'):
            params['id_token_hint'] = self.previous['id_token']
        return ISSUER + '/api/accounts/authorize?' + urlencode(params)

    def complete(self, query):
        if (self.consumed or time.monotonic() > self.deadline or
                not secrets.compare_digest(query.get('state', ''), self.state)):
            raise ValueError('subscription_callback_state_invalid')
        self.consumed = True
        if query.get('error'):
            raise ValueError('subscription_authorization_declined')
        issued = query.get('client_id', self.previous.get('client_id', ''))
        if not issued.startswith('oaiapp_') or not query.get('code'):
            raise ValueError('subscription_registration_incomplete')
        if self.previous.get('client_id') and issued != self.previous['client_id']:
            raise ValueError('subscription_registration_changed')
        response = self.client.post(TOKEN_URL, data=dict(grant_type='authorization_code',
            client_id=issued, code=query['code'], code_verifier=self.verifier,
            redirect_uri=self.redirect_uri, resource=RESOURCE))
        if response.status_code != 200:
            raise ValueError('subscription_code_exchange_failed')
        tokens = response.json()
        keys = self.client.get(ISSUER + '/.well-known/jwks.json')
        if keys.status_code != 200:
            raise ValueError('subscription_signing_keys_unavailable')
        header = jwt.get_unverified_header(tokens['id_token'])
        matches = [key for key in keys.json()['keys'] if key.get('kid') == header.get('kid')]
        if len(matches) != 1:
            raise ValueError('subscription_signing_key_unknown')
        claims = jwt.decode(tokens['id_token'], jwt.PyJWK.from_dict(matches[0]).key,
            algorithms=['RS256'], issuer=ISSUER, audience=issued, leeway=5,
            options={'require': ['sub', 'exp', 'iat', 'nonce']})
        if claims['nonce'] != self.nonce or not isinstance(claims['sub'], str) or not claims['sub']:
            raise ValueError('subscription_identity_invalid')
        if self.previous.get('subject') and claims['sub'] != self.previous['subject']:
            raise ValueError('subscription_account_changed')
        value = token_record(tokens, dict(client_id=issued, issuer=ISSUER,
            subject=claims['sub'], email=claims.get('email'), ext_agent_host_id=self.host_id))
        with credential_lock(self.path):
            write_private(self.path, value)
