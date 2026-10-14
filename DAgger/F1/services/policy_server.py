#!/usr/bin/env python3
"""Server-only model adapter. --mock is explicit and never hardware acceptance."""
import argparse
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import math
import time
# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

from f1dagger.session import capabilities, CONTRACT, CAMERAS

def serve(host,port,predict,model_id,version,steps,delay=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def send(self,status,obj):
            raw=json.dumps(obj,allow_nan=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            self.send(200,capabilities(model_id,version,steps)) if self.path=='/health' else self.send(404,{'error':'not found'})
        def do_POST(self):
            try:
                if self.path!='/process_frame':raise ValueError('unknown route')
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<=40*1024*1024:raise ValueError('body size')
                self.connection.settimeout(5)
                body=self.rfile.read(length)
                msg=BytesParser(policy=default).parsebytes(('Content-Type: '+self.headers.get('Content-Type','')+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
                parts={}
                for part in msg.iter_parts():
                    name=part.get_param('name',header='content-disposition')
                    if name in parts:raise ValueError('duplicate multipart field')
                    parts[name]=part.get_payload(decode=True)
                if set(parts)!=set(CAMERAS+['metadata']):raise ValueError('three images and metadata required')
                m=json.loads(parts.pop('metadata'))
                if m['contract']!=CONTRACT or m['frequency_hz']!=30 or m['chunk_steps']!=steps:raise ValueError('contract/timeline mismatch')
                if len(m['state16'])!=16 or any(type(v) not in (int,float) or not math.isfinite(v) for v in m['state16']):raise ValueError('invalid state16')
                # Model-specific crop/normalization/weights and de-normalization
                # live in predict. Return hardware degrees + 0..100 absolute targets.
                if delay:time.sleep(delay)
                actions=predict(parts,m)
                if len(actions)!=steps or any(len(row)!=16 or any(type(v) not in (int,float) or not math.isfinite(v) for v in row) for row in actions):raise ValueError('adapter must return finite Hx16 list')
                result={k:m[k] for k in ('contract','request_id','observation_id','policy_epoch','observation_monotonic_ns','frequency_hz')}
                self.send(200,dict(result,model_id=model_id,model_version=version,actions=actions))
            except Exception as exc:self.send(400,dict(error=str(exc),contract=CONTRACT))
    ThreadingHTTPServer((host,port),Handler).serve_forever()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8091)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--mock',action='store_true');g.add_argument('--adapter',help='module:function')
    p.add_argument('--model-id',default='mock-hold');p.add_argument('--model-version',default='mock-v1');p.add_argument('--steps',type=int,default=50);p.add_argument('--delay',type=float,default=0)
    a=p.parse_args()
    if a.mock:predict=lambda images,m:[list(m['state16']) for _ in range(a.steps)]
    else:
        module,function=a.adapter.split(':');predict=getattr(importlib.import_module(module),function)
        if a.model_id=='mock-hold' or a.model_version=='mock-v1':p.error('provide real model id and immutable version')
    serve(a.host,a.port,predict,a.model_id,a.model_version,a.steps,a.delay)
