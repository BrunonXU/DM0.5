#!/usr/bin/env python3
"""SSH-friendly browser knobs; playback happens on F1, never in the browser."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
from urllib.parse import urlsplit
sys.path.insert(0, os.environ.get('F1_AUDIO_MODULE_ROOT', str(Path(__file__).resolve().parents[1]/'src')))
from f1dagger.recording_audio import DEFAULT_CONFIG, levels, save_levels, play

PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>F1 · 提示音调节</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f4f6;color:#222;font:17px system-ui,sans-serif}main{max-width:800px;margin:55px auto;padding:30px}h1{font-size:30px;margin:0 0 10px}p{color:#666;line-height:1.6}.cards{display:flex;gap:24px;margin:28px 0}article{flex:1;background:white;padding:28px;border-radius:22px;text-align:center}h2{font-size:21px;margin:0 0 22px}.knob{width:148px;height:148px;border-radius:50%;position:relative;margin:0 auto;touch-action:none;cursor:grab;background:conic-gradient(from 225deg,#458cd7 var(--angle),#e3e7ec 0deg 270deg,transparent 270deg)}.face{position:absolute;inset:10px;border-radius:50%;background:#f8f9fb;box-shadow:0 2px 7px #0002;pointer-events:none}.needle{position:absolute;left:calc(50% - 2px);top:8px;width:4px;height:24px;border-radius:4px;background:#458cd7;transform-origin:2px 56px;transform:rotate(var(--rotation));pointer-events:none}.value{padding:18px 0;font-size:23px;font-weight:600}button{border:1px solid #d5d9e0;border-radius:12px;background:white;padding:11px 22px;font-size:17px;cursor:pointer}button:disabled{opacity:.4;cursor:default}.save{background:#2464a5;color:white;border:0}#status{min-height:52px}.knob:focus-visible{outline:3px solid #aac9ec;outline-offset:5px}@media(max-width:600px){main{padding:18px;margin:18px auto}.cards{gap:12px}article{padding:18px 8px}.knob{width:136px;height:136px}.needle{transform-origin:2px 50px}}
</style><main><h1>录制提示音</h1><p>点击播放，声音从 <b>F1 扬声器</b>发出。拖动旋钮调音量，0% 为静音。</p><div class="cards">
<article data-kind="started"><h2>开始 A</h2><div class="knob" role="slider" tabindex="0" aria-label="开始音量" aria-valuemin="0" aria-valuemax="100"><div class="face"><i class="needle"></i></div></div><div class="value"></div><button class="play">▶ 播放</button></article>
<article data-kind="saved"><h2>完成 B</h2><div class="knob" role="slider" tabindex="0" aria-label="完成音量" aria-valuemin="0" aria-valuemax="100"><div class="face"><i class="needle"></i></div></div><div class="value"></div><button class="play">▶ 播放</button></article></div><button class="save" disabled>保存音量</button><p id="status" role="status">正在连接…</p><p style="font-size:14px">保存后，连续采集从下一次提示音起使用新音量。无需 NoMachine，不改系统总音量。调好后可关闭页面，并在启动终端按 Ctrl+C 退出调音工具。</p></main><script>
const token=location.hash.slice(1)||sessionStorage.getItem('f1-audio-token')||'';if(token)sessionStorage.setItem('f1-audio-token',token);history.replaceState(null,'',location.pathname);const value={started:18,saved:18};let busy=false,ready=false;const status=document.querySelector('#status');
async function request(path,body){const r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-F1-Audio-Token':token},...(body?{body:JSON.stringify(body)}:{})});const j=await r.json();if(!r.ok)throw Error(j.error||'请求失败');return j;}
function paint(card,v){const k=card.dataset.kind;value[k]=Math.max(0,Math.min(100,Math.round(v)));const knob=card.querySelector('.knob');knob.style.setProperty('--angle',value[k]*2.7+'deg');knob.style.setProperty('--rotation',(-135+value[k]*2.7)+'deg');knob.setAttribute('aria-valuenow',value[k]);card.querySelector('.value').textContent=value[k]+'%';}
function buttons(){document.querySelectorAll('.play').forEach(b=>b.disabled=!ready||busy);}
async function poll(){try{const j=await request('state');busy=j.busy;buttons();if(busy)setTimeout(poll,250);else status.textContent=j.error?'播放失败：'+j.error:'播放已结束；请确认现场是否听到。';}catch(e){busy=false;buttons();status.textContent=e.message;}}
document.querySelectorAll('article').forEach(card=>{const knob=card.querySelector('.knob');paint(card,value[card.dataset.kind]);const set=e=>{if(!ready)return;const r=knob.getBoundingClientRect();let a=(Math.atan2(e.clientY-r.top-r.height/2,e.clientX-r.left-r.width/2)*180/Math.PI+450)%360;let d=(a+135)%360;if(d>270)d=d>315?0:270;paint(card,d/2.7);};knob.onpointerdown=e=>{knob.setPointerCapture(e.pointerId);set(e);};knob.onpointermove=e=>{if(knob.hasPointerCapture(e.pointerId))set(e);};knob.onkeydown=e=>{if(!ready)return;if(['ArrowLeft','ArrowDown','ArrowRight','ArrowUp'].includes(e.key)){e.preventDefault();paint(card,value[card.dataset.kind]+(['ArrowLeft','ArrowDown'].includes(e.key)?-1:1));}};card.querySelector('.play').onclick=async()=>{busy=true;buttons();status.textContent='正在从 F1 播放…';try{await request('play',{kind:card.dataset.kind,volume:value[card.dataset.kind]});poll();}catch(e){busy=false;buttons();status.textContent=e.message;}};});
document.querySelector('.save').onclick=async()=>{try{await request('save',value);status.textContent='已保存，采集提示音使用此音量。';}catch(e){status.textContent=e.message;}};
request('state').then(j=>{document.querySelectorAll('article').forEach(c=>paint(c,j.levels[c.dataset.kind]));ready=true;busy=j.busy;buttons();document.querySelector('.save').disabled=false;status.textContent='可以试听。旋钮也支持键盘方向键。';if(busy)poll();}).catch(e=>{buttons();status.textContent='连接失败：'+e.message+'。请打开启动终端打印的完整链接。';});buttons();
</script></html>'''


def make_server(address, config, *, token=None, player=play):
    token = token or secrets.token_urlsafe(24)
    lock = threading.Lock()
    state = {'busy': False, 'error': None}
    def volume(v):
        if type(v) is not int or not 0 <= v <= 100:
            raise ValueError('音量必须是 0–100 的整数')
        return v
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):pass
        def reply(self, code, body, mime='application/json'):
            data = body.encode() if isinstance(body,str) else json.dumps(body,ensure_ascii=False).encode()
            self.send_response(code);self.send_header('Content-Type',mime+'; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Frame-Options','DENY');self.end_headers();self.wfile.write(data)
        def authorized(self):
            return hmac.compare_digest(self.headers.get('X-F1-Audio-Token',''),token)
        def do_GET(self):
            if self.path=='/':self.reply(200,PAGE,'text/html');return
            if not self.authorized():self.reply(403,{'error':'请使用终端打印的完整链接'});return
            if self.path=='/api/state':
                with lock:result=dict(state,levels=levels(config))
                self.reply(200,result)
            else:self.reply(404,{'error':'未知接口'})
        def do_POST(self):
            origin=self.headers.get('Origin')
            if not self.authorized() or (origin and urlsplit(origin).netloc!=self.headers.get('Host')):
                self.reply(403,{'error':'访问校验失败'});return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=1024:raise ValueError('请求长度无效')
                body=json.loads(self.rfile.read(size))
                if self.path=='/api/save':
                    validated={k:volume(body[k]) for k in ('started','saved')}
                    with lock:save_levels(config,validated)
                    self.reply(200,{'saved':True});return
                if self.path!='/api/play':self.reply(404,{'error':'未知接口'});return
                kind=body['kind'];v=volume(body['volume'])
                if kind not in ('started','saved'):raise ValueError('未知提示音')
                with lock:
                    if state['busy']:self.reply(409,{'error':'上一段正在播放，请稍等'});return
                    state.update(busy=True,error=None)
                def work():
                    error=None
                    try:player(kind,v)
                    except Exception as exc:error=str(exc)[:250]
                    with lock:state.update(busy=False,error=error)
                threading.Thread(target=work,daemon=True).start()
                self.reply(202,{'playing':True})
            except (ValueError,KeyError,TypeError) as exc:self.reply(400,{'error':str(exc)})
            except OSError as exc:self.reply(500,{'error':str(exc)})
    server=ThreadingHTTPServer(address,Handler);server.daemon_threads=True
    return server,token


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=4243)
    args=parser.parse_args()
    try:server,token=make_server((args.host,args.port),args.config)
    except OSError as exc:parser.exit(1,'无法启动调音工具：'+str(exc)+'\n可使用 --port 4244 换端口。\n')
    host=args.host
    if host=='0.0.0.0':
        host=socket.gethostname()
        try:
            addresses=json.loads(subprocess.check_output(['ip','-j','-4','address'],text=True))
            ips=[a['local'] for d in addresses for a in d.get('addr_info',[]) if a.get('scope')=='global']
            host=next((ip for ip in ips if ip.startswith('192.168.31.')),ips[0] if ips else host)
        except (OSError,ValueError,KeyError,subprocess.SubprocessError):pass
    print(f'\n请在电脑浏览器打开完整链接：\nhttp://{host}:{server.server_port}/#{token}\n\n声音从 F1 播放；保存音量后按 Ctrl+C 退出即可。\n',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
