"""On-demand model exchange evidence; never stores HTTP headers or credentials."""
import base64
import hashlib
import json
import re
import time
from pathlib import Path

LIMIT = 1800 * 1024
IDENTITY = r'[a-f0-9]{32}'
SECRET = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9_.~+/-]+=*)', re.I)
SENSITIVE = re.compile(r'^(?:authorization|api[_-]?key|password|secret|credential|cookie|access_token|refresh_token)$', re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: '[已隐藏凭据]' if SENSITIVE.fullmatch(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return SECRET.sub('[已隐藏凭据]', value)
    return value


def capture(store, request_id, part, value):
    """Supplemental UI evidence cannot cause a motion or replace a task failure."""
    if not hasattr(store, 'root'):
        return  # Event-only stores do not support supplemental files.
    try:
        assert re.fullmatch(IDENTITY, request_id)
        assert re.fullmatch(r'(request|response|attempt-[12]|tool-[a-f0-9]{16})', part)
        def images(v):
            if isinstance(v, dict): return {k: images(x) for k, x in v.items()}
            if isinstance(v, list): return [images(x) for x in v]
            if isinstance(v, str) and v.startswith('data:image/'):
                prefix, data = v.split(';base64,', 1)
                if prefix not in ('data:image/png', 'data:image/jpeg', 'data:image/webp'):
                    raise ValueError('unsupported_image_type')
                raw = base64.b64decode(data, validate=True)
                return dict(image_ref=store.put_blob(raw), mime=prefix[5:], bytes=len(raw))
            return v
        row = dict(request_id=request_id, part=part, monotonic_ns=time.monotonic_ns(),
                   content=redact(images(value)), redacted=True)
        encoded = json.dumps(row, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > LIMIT: raise ValueError('exchange_capture_size_limit')
        # Atomic visibility to a live reader. Unique part names are immutable.
        path = store.root / ('llm-' + request_id + '-' + part + '.json')
        if path.exists(): raise ValueError('exchange_part_already_exists')
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(encoded)
        tmp.replace(path)
    except Exception as exc:
        try: store.event('llm_capture_unavailable', request_id=request_id,part=part,error_type=type(exc).__name__)
        except Exception: pass


def _read(path):
    if path.is_symlink() or path.stat().st_size > LIMIT:
        raise ValueError('交互记录不符合读取约定。')
    return json.loads(path.read_text())


def exchanges(root, *, started_ns=0, request_id=None, image_id=None):
    root = Path(root)
    if root.is_symlink(): raise ValueError('运行目录无效。')
    if request_id is None:
        rows = []
        for p in root.glob('llm-*-request.json'):
            rid = p.name[4:-13]
            if not re.fullmatch(IDENTITY, rid): continue
            req = _read(p)
            response = root / ('llm-' + rid + '-response.json')
            returned = _read(response) if response.exists() else None
            attempts = [_read(x)['content'] for x in sorted(root.glob('llm-' + rid + '-attempt-*.json'))]
            rows.append(dict(request_id=rid, time_s=(req['monotonic_ns']-started_ns)/1e9,
                status='returned' if returned else 'error' if attempts and not attempts[-1].get('automatic_retry') else 'waiting',
                elapsed_s=(returned['monotonic_ns']-req['monotonic_ns'])/1e9 if returned else None,
                model=req['content'].get('model'), attempts=len(attempts),
                usage=(returned or {}).get('content', {}).get('usage')))
        return dict(exchanges=sorted(rows, key=lambda r:r['time_s']), complete_capture=bool(rows),
                    note='' if rows else '本次没有完整模型交互记录；可能尚未调用模型，或属于记录功能上线前的运行。')
    if not re.fullmatch(IDENTITY, request_id): raise ValueError('模型请求身份无效。')
    req = _read(root / ('llm-' + request_id + '-request.json'))
    parts = [_read(p) for p in sorted(root.glob('llm-' + request_id + '-*.json'))]
    if image_id is not None:
        if not re.fullmatch(r'[a-f0-9]{64}', image_id): raise ValueError('图像身份无效。')
        def refs(v):
            if isinstance(v, dict):
                if v.get('image_ref') == image_id: yield v
                for x in v.values(): yield from refs(x)
            elif isinstance(v, list):
                for x in v: yield from refs(x)
        ref = next(refs(parts), None)
        if ref is None: raise ValueError('图片不属于本次交互。')
        path = root / 'blobs' / image_id
        if (root/'blobs').is_symlink() or path.is_symlink() or path.stat().st_size > LIMIT * .7:
            raise ValueError('图片超过网页读取上限，原件保持。')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != image_id: raise ValueError('图像校验失败。')
        return dict(data_url='data:' + ref['mime'] + ';base64,' + base64.b64encode(data).decode())
    result = dict(request=req, parts=[p for p in parts if p['part'] != 'request'])
    if len(json.dumps(result).encode()) > LIMIT: raise ValueError('完整交互超过网页读取上限，原件保持。')
    return result
