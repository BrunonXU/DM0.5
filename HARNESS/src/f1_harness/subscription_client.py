"""SIWC Responses transport, adapted to the existing completed-response client."""
import copy
import json
import time

import httpx
from .subscription_auth import RESOURCE, access_token

NAMESPACE = 'harness'
MAX_BYTES = 8 * 1024**2


def subscription_payload(payload):
    value = copy.deepcopy(payload)
    # The only unsupported field emitted by our existing planner.
    value.pop('max_output_tokens', None)
    value.update(store=False, stream=True)
    if not isinstance(value.get('input'), list):
        raise ValueError('subscription_requires_full_history')
    tools = value.get('tools', [])
    if any(tool.get('type') != 'function' for tool in tools):
        raise ValueError('subscription_tool_contract_unsupported')
    value['tools'] = [dict(type='namespace', name=NAMESPACE,
        description='F1 Harness existing program and completion tools.', tools=tools)] if tools else []
    for item in value['input']:
        if item.get('type') == 'function_call':
            if item.get('namespace') not in (None, NAMESPACE):
                raise ValueError('subscription_tool_namespace_mismatch')
            item['namespace'] = NAMESPACE
    # SIWC requires namespaced tools; a bare forced-function choice is rejected.
    # Our protocol smoke exposes exactly one tool, so required is equivalent.
    choice = value.get('tool_choice')
    if isinstance(choice, dict):
        if (choice.get('type') != 'function' or len(tools) != 1 or
                choice.get('name') != tools[0]['name']):
            raise ValueError('subscription_forced_choice_unsupported')
        value['tool_choice'] = 'required'
    return value


def completed_response(response, *, deadline):
    """Never expose partial tool calls, failed responses, or interrupted streams."""
    # The observed public SIWC response omitted Content-Type but contained valid
    # SSE through response.completed. Validate framing/terminal data regardless.
    if response.headers.get('content-type', '').split(';')[0] not in ('', 'text/event-stream'):
        raise RuntimeError('subscription_stream_content_type_invalid')
    buffered = b''
    total = 0
    data = []
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_BYTES or time.monotonic() > deadline:
            raise RuntimeError('subscription_stream_limit_no_replay')
        buffered += chunk
        while b'\n' in buffered:
            line, buffered = buffered.split(b'\n', 1)
            line = line.rstrip(b'\r')
            if line.startswith(b'data:'):
                data.append(line[5:].lstrip(b' '))
            elif not line and data:
                raw = b'\n'.join(data)
                data = []
                if raw == b'[DONE]':
                    raise RuntimeError('subscription_stream_incomplete_no_replay')
                event = json.loads(raw)
                kind = event.get('type')
                if kind == 'response.completed':
                    value = event['response']
                    if value.get('status') != 'completed':
                        raise RuntimeError('subscription_stream_incomplete_no_replay')
                    for item in value.get('output', []):
                        if item.get('type') == 'function_call' and item.get('namespace') != NAMESPACE:
                            raise RuntimeError('subscription_tool_namespace_mismatch')
                    return value
                if kind in ('response.failed', 'response.incomplete', 'error'):
                    # Never propagate provider messages containing reflected inputs.
                    error = event.get('response', {}).get('error') or event.get('error') or event
                    code = error.get('code') if isinstance(error, dict) else None
                    if code in ('subscription_sharing_usage_limit_exceeded',
                                'subscription_sharing_usage_unavailable'):
                        raise RuntimeError(code)
                    raise RuntimeError('subscription_stream_failed_no_replay')
    raise RuntimeError('subscription_stream_incomplete_no_replay')


class SubscriptionClient:
    """Explicitly selected backup. No API-key fallback or POST replay."""
    def __init__(self, credentials, *, timeout=None, client=None):
        self.credentials = credentials
        self.timeout = timeout or httpx.Timeout(90, connect=5, write=15, pool=5)
        self._owns_client = client is None
        self.client = client or self._new_client()
        self.catalog_checked_at = 0.

    def _new_client(self):
        return httpx.Client(base_url=RESOURCE, timeout=self.timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=120))

    def _headers(self):
        return {'Authorization': 'Bearer ' + access_token(self.credentials, self.client)}

    def get(self, path, **kwargs):
        from .planner import MODEL
        if path != '/models/' + MODEL:
            raise ValueError('subscription_endpoint_rejected')
        try:
            response = self.client.get('/models', headers=self._headers(), **kwargs)
        except httpx.HTTPError:
            # Match the pool's existing poisoned proxy-tunnel recovery. This
            # was a catalog/refresh request, never inference; no replay here.
            if self._owns_client:
                self.client.close()
                self.client = self._new_client()
            raise
        if response.status_code == 200:
            catalog = response.json().get('models', [])
            if not any(item.get('slug') == MODEL for item in catalog):
                raise RuntimeError('subscription_astra_not_available')
            self.catalog_checked_at = time.monotonic()
        return response

    def post(self, path, *, json, headers, extensions):
        from .planner import MODEL, EFFORT
        if (path != '/responses' or json.get('model') != MODEL or
                json.get('reasoning', {}).get('effort') != EFFORT or
                json.get('service_tier', 'default') != 'default'):
            raise ValueError('subscription_model_contract_rejected')
        if time.monotonic() - self.catalog_checked_at > 60:
            catalog = self.get('/models/' + MODEL, timeout=5)
            if catalog.status_code != 200:
                return httpx.Response(catalog.status_code, json={'error': {'code': 'subscription_catalog_failed'}})
        request_headers = dict(headers, **self._headers())
        payload = subscription_payload(json)
        deadline = time.monotonic() + (self.timeout.read or 90)
        with self.client.stream('POST', path, json=payload, headers=request_headers,
                                extensions=extensions) as response:
            diagnostics = {k: response.headers[k] for k in ('x-request-id', 'openai-processing-ms')
                           if k in response.headers}
            diagnostics['x-f1-model-auth'] = 'chatgpt_subscription'
            if response.status_code != 200:
                return httpx.Response(response.status_code, headers=diagnostics,
                                      json={'error': {'code': 'subscription_http_rejected'}})
            value = completed_response(response, deadline=deadline)
            return httpx.Response(200, headers=diagnostics, json=value)

    def close(self):
        self.client.close()
