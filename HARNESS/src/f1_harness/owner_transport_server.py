"""Private stream framing. This service is never registered in the planner RPC."""
import json

MAX_MESSAGE = 65536
METHODS = ('status', 'web_start', 'web_stop', 'web_control_status', 'web_close_session')


def decode(body, streaming):
    if len(body) > MAX_MESSAGE:
        raise ValueError('request_too_large')
    request = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    fields = {'method', 'kwargs', 'id'} if streaming else {'method', 'kwargs'}
    if (not isinstance(request, dict) or set(request) != fields
            or not isinstance(request['kwargs'], dict) or request['method'] not in METHODS):
        raise ValueError('private_web_method_required')
    if streaming and (not isinstance(request['id'], str) or len(request['id']) != 32
                      or any(c not in '0123456789abcdef' for c in request['id'])):
        raise ValueError('request_identity_required')
    return request


def serve(client, source, output, errors, *, streaming):
    while True:
        body = source.readline(MAX_MESSAGE + 2) if streaming else source.read(MAX_MESSAGE + 1)
        if streaming and not body:
            return 0
        if streaming:
            if not body.endswith(b'\n'):
                raise ValueError('incomplete_private_transport_frame')
            body = body[:-1]
        request = decode(body, streaming)
        try:
            result = client.call(request['method'], **request['kwargs'])
            reply = dict(id=request['id'], ok=True, result=result) if streaming else result
        except Exception as exc:
            error = dict(error_type=type(exc).__name__, error=str(exc)[:1000])
            if not streaming:
                print(json.dumps(error), file=errors)
                return 1
            reply = dict(id=request['id'], ok=False, **error)
        encoded = json.dumps(reply, allow_nan=False)
        if len(encoded.encode()) > MAX_MESSAGE:
            raise ValueError('private_reply_too_large')
        print(encoded, file=output, flush=True)
        if not streaming:
            return 0
