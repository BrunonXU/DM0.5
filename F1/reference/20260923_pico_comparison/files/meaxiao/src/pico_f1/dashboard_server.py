"""Loopback-only data review server, isolated from teleoperation and recording."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pyarrow.parquet as pq
from pico_f1.episode_analysis import analyze_episode, clean, episode_paths, population_statistics, phase_statistics


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(clean(value), ensure_ascii=False, allow_nan=False))
    temp.replace(path)


class ReviewStore:
    def __init__(self, root, cache, logs, probe=True):
        self.root_link = Path(root).absolute()
        self.root, self.cache, self.logs = map(lambda p: Path(p).resolve(), (root, cache, logs))
        if self.root == self.cache or self.root in self.cache.parents:
            raise ValueError('analysis cache must be outside recording dataset')
        self.probe = probe
        self.lock = threading.RLock()
        self.episodes, self.signatures, self.paths = {}, {}, {}
        self.error, self.updated = None, None
        self.events = []
        self.sync_status_file = None

    def refresh(self):
        # The central multi-robot merger publishes through one atomic symlink.
        self.root = self.root_link.resolve()
        info_path = self.root/'meta/info.json'
        if not info_path.exists():
            with self.lock: self.error = '尚无已提交的数据集 meta/info.json'
            return
        raw = info_path.read_bytes()
        info = json.loads(raw)
        count = int(info['total_episodes'])
        rows = []
        for p in sorted((self.root/'meta/episodes').rglob('*.parquet')):
            rows += [r for r in pq.read_table(p).to_pylist() if int(r['episode_index']) < count]
        ids = [int(r['episode_index']) for r in rows]
        if len(ids) != count or sorted(ids) != list(range(count)):
            raise ValueError('committed episode index/count mismatch; retrying without mixed snapshot')
        if sum(int(r['length']) for r in rows) != int(info['total_frames']):
            raise ValueError('committed metadata frame total mismatch')
        results, signatures, paths = {}, {}, {}
        provenance_file = self.root/'provenance.json'
        provenance = {int(r['global_episode']): r for r in json.loads(provenance_file.read_text())['episodes']} if provenance_file.exists() else {}
        for row in rows:
            eid = int(row['episode_index'])
            data, videos = episode_paths(self.root, row, info)
            annotation = self.cache/f'annotation-{eid:06d}.json'
            dependencies = [data, *videos, self.root/'alignment'/f'episode-{eid:06d}'/'extras.jsonl', annotation]
            signature = hashlib.sha256(json.dumps(['analysis-1.4.2', row, info['features'], self.probe, provenance.get(eid),
                [(str(p.relative_to(self.root)) if p.is_relative_to(self.root) else str(p), p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else (p.name, None)
                 for p in dependencies]], sort_keys=True).encode()).hexdigest()
            if self.signatures.get(eid) == signature:
                results[eid] = self.episodes[eid]
            else:
                try:
                    d = analyze_episode(self.root, row, info, self.probe)
                    if eid in provenance:
                        d['provenance'] = provenance[eid]
                        d['summary']['robot_id'] = provenance[eid]['robot_id']
                        d['summary']['source_episode'] = provenance[eid]['source_episode']
                    recording_log = self.logs/'recorder.log'
                    log_lines = []
                    if recording_log.exists():
                        with recording_log.open('rb') as f:
                            f.seek(max(0, recording_log.stat().st_size - 2_000_000))
                            start_s = d['summary']['source_start_ns']/1e9
                            end_s = start_s + d['summary']['duration_s'] + 30
                            for line in f.read().decode(errors='replace').splitlines():
                                m = re.search(r'\[(\d{10}\.\d+)\]', line)
                                if m and start_s-3 <= float(m[1]) <= end_s:
                                    log_lines.append(line)
                    d['recording_log'] = log_lines[-200:]
                    d['recording_log_scope'] = '录制源时间前3秒至结束后30秒的当前日志摘录；不代表完整历史事件'
                    d['analyzed_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    if annotation.exists():
                        ann = json.loads(annotation.read_text())
                        if ann.get('data_sha256') == hashlib.sha256(data.read_bytes()).hexdigest():
                            b = ann['boundaries']
                            d['stages'] = [dict(name=name, start=b[i], end=b[i+1], inferred=False)
                                for i, name in enumerate(['1 抓取手机','2 移动到放置点','3 放置手机','4 返回并复位'])]
                            d['stage_note'] = '人工确认边界（不自动等同任务成功）'
                            d['summary']['stage_status'] = '人工确认'
                            d['phase_metrics'] = phase_statistics(d['curves'], d['stages'])
                    atomic_json(self.cache/f'episode-{eid:06d}.json', d)
                    results[eid] = d
                    event = dict(time=time.strftime('%Y-%m-%d %H:%M:%S'), episode=eid,
                                 event='ANALYZED', quality=d['summary']['quality'])
                    self.events.append(event)
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                except Exception as exc:
                    results[eid] = dict(summary=dict(episode=eid, frames=int(row['length']),
                        quality='分析失败', issues=[str(exc)], duration_s=None), curves=[], metrics=[],
                        stages=[], cameras=[], error=str(exc))
                # Failed episodes retry next scan rather than becoming permanently cached.
            if not results[eid].get('error'): signatures[eid] = signature
            paths[eid] = (data, videos)
        if raw != info_path.read_bytes():
            raise RuntimeError('new commit during scan; retrying consistent snapshot')
        with self.lock:
            self.episodes, self.signatures, self.paths = results, signatures, paths
            self.error = None
            self.updated = time.strftime('%Y-%m-%d %H:%M:%S')
            self.events = self.events[-200:]

    def summary(self):
        with self.lock:
            rows = [self.episodes[k]['summary'] for k in sorted(self.episodes)]
            error,updated=self.error,self.updated
            if self.sync_status_file:
                try:
                    sync=json.loads(self.sync_status_file.read_text())
                    stamp=sync.get('last_success')
                    updated=f'{self.updated} / SSH '+(time.strftime('%H:%M:%S',time.localtime(stamp)) if stamp else '尚未完成')
                    if sync.get('error') or not stamp or time.time()-stamp > 90:
                        error='SSH同步异常或超时，显示上次快照：'+str(sync.get('error') or '等待同步')
                except (OSError,ValueError):error='等待首次SSH同步；暂无新鲜来源保证'
            return dict(rows=rows, population=population_statistics(rows), updated=updated,
                revision=hashlib.sha256(str(sorted(self.signatures.items())).encode()).hexdigest(),
                error=error, events=self.events[-50:], root_name=self.root.name)

    def annotation(self, eid, boundaries):
        with self.lock:
            d = self.episodes[eid]
            end = d['summary']['duration_s']
            if (not isinstance(boundaries, list) or len(boundaries) != 5
                or any(not isinstance(x, (int, float)) or not __import__('math').isfinite(x) for x in boundaries)
                or boundaries[0] != 0 or abs(boundaries[-1] - end) > .001
                or any(b <= a for a, b in zip(boundaries, boundaries[1:]))):
                raise ValueError('five finite increasing boundaries from 0 to source duration required')
            data = self.paths[eid][0]
            atomic_json(self.cache/f'annotation-{eid:06d}.json', dict(boundaries=boundaries,
                data_sha256=hashlib.sha256(data.read_bytes()).hexdigest(), confirmed_at=time.time()))


def handler_for(store, dist, auth_token=None):
    dist = Path(dist).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def authenticated(self):
            if auth_token is None: return True
            cookie = SimpleCookie()
            try: cookie.load(self.headers.get('Cookie', ''))
            except Exception: return False
            value = cookie.get('pico_review_session')
            supplied = value.value if value else ''
            header = self.headers.get('Authorization', '')
            if header.startswith('Bearer '): supplied = header[7:]
            return supplied.isascii() and secrets.compare_digest(supplied, auth_token)

        def login_page(self):
            body = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>PICO 私有数据登录</title>
<h2>PICO 私有数采网页</h2><p>仅通过 localhost / SSH 隧道访问。输入本账号私有 access.token 文件中的密钥；不要放入 URL。</p>
<form id="login"><input id="token" type="password" autocomplete="off" aria-label="访问密钥" required><button>登录</button></form><p id="status"></p>
<script>document.getElementById('login').onsubmit=async e=>{e.preventDefault();let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.getElementById('token').value})});if(r.ok)location.reload();else document.getElementById('status').textContent='密钥无效';};</script></html>'''.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Frame-Options','DENY')
            self.end_headers(); self.wfile.write(body)

        def allowed(self):
            host = self.headers.get('Host', '')
            permitted = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            return host in permitted and self.headers.get('Origin', 'http://'+host) == 'http://'+host

        def send_json(self, data, status=200):
            body = json.dumps(clean(data), ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed(): return self.send_json({'error': 'loopback origin only'}, 403)
            path = urlsplit(self.path).path
            if not self.authenticated():
                if path == '/': return self.login_page()
                return self.send_json({'error':'authentication required'},401)
            try:
                if path == '/api/review': return self.send_json(store.summary())
                if path == '/api/logs':
                    result = {}
                    for name in ('recorder.log', 'teleop.log', 'dashboard.log', 'receiver.log'):
                        p = store.logs/name
                        if p.exists():
                            with p.open('rb') as f:
                                f.seek(max(0, p.stat().st_size - 24000))
                                result[name] = f.read().decode(errors='replace')
                    return self.send_json(dict(logs=result, note='当前会话日志末尾；旧日志若已被覆盖不可追溯'))
                match = re.fullmatch(r'/api/episode/(\d+)', path)
                if match:
                    with store.lock: data = store.episodes[int(match[1])]
                    return self.send_json(data)
                match = re.fullmatch(r'/api/video/(\d+)/([0-2])', path)
                if match:
                    with store.lock: p = store.paths[int(match[1])][1][int(match[2])]
                    return self.send_file(p, 'video/mp4')
                if path == '/': p = dist/'index.html'
                elif re.fullmatch(r'/[A-Za-z0-9_.-]+\.json', path): p = dist/path[1:]
                else: return self.send_json({'error': 'not found'}, 404)
                return self.send_file(p, 'text/html' if path == '/' else 'application/json')
            except (KeyError, FileNotFoundError): return self.send_json({'error': 'not found'}, 404)
            except (BrokenPipeError, ConnectionResetError): return
            except Exception as exc: return self.send_json({'error': str(exc)}, 500)

        def send_file(self, path, content_type):
            size = path.stat().st_size
            start, end, status = 0, size-1, 200
            request_range = self.headers.get('Range')
            if request_range:
                m = re.fullmatch(r'bytes=(\d+)-(\d*)', request_range)
                if not m: return self.send_json({'error': 'unsupported range'}, 416)
                start, end = int(m[1]), min(int(m[2]) if m[2] else end, end)
                if start > end: return self.send_json({'error': 'range outside file'}, 416)
                status = 206
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(end-start+1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            if status == 206: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            with path.open('rb') as f:
                f.seek(start)
                remaining = end-start+1
                while remaining:
                    data = f.read(min(65536, remaining))
                    if not data: break
                    self.wfile.write(data)
                    remaining -= len(data)

        def do_POST(self):
            if not self.allowed(): return self.send_json({'error': 'loopback origin only'}, 403)
            if urlsplit(self.path).path == '/api/login' and auth_token is not None:
                try:
                    size = int(self.headers.get('Content-Length',0))
                    if not 0 < size < 4096: raise ValueError('invalid size')
                    supplied = json.loads(self.rfile.read(size)).get('token','')
                    if not isinstance(supplied,str) or not supplied.isascii() or not secrets.compare_digest(supplied,auth_token):
                        return self.send_json({'error':'invalid token'},401)
                    self.send_response(204)
                    self.send_header('Set-Cookie',f'pico_review_session={auth_token}; HttpOnly; SameSite=Strict; Path=/')
                    self.send_header('Cache-Control','no-store'); self.end_headers(); return
                except Exception: return self.send_json({'error':'invalid login'},400)
            if not self.authenticated(): return self.send_json({'error':'authentication required'},401)
            match = re.fullmatch(r'/api/annotations/(\d+)', urlsplit(self.path).path)
            if not match: return self.send_json({'error': 'not found'}, 404)
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size < 4096: raise ValueError('invalid request size')
                body = json.loads(self.rfile.read(size))
                store.annotation(int(match[1]), body['boundaries'])
                return self.send_json({'status': 'saved_sidecar_only'})
            except Exception as exc: return self.send_json({'error': str(exc)}, 400)

    return Handler


def main():
    p = argparse.ArgumentParser()
    project = Path(__file__).resolve().parents[2]
    p.add_argument('--root', type=Path, default=project/'mocap_data')
    p.add_argument('--cache', type=Path, default=project/'reports/dashboard_cache')
    p.add_argument('--dist', type=Path, default=project/'dashboard/dist')
    p.add_argument('--logs', type=Path, default=project/'logs')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--no-video-probe', action='store_true')
    p.add_argument('--auth-token-file', type=Path)
    p.add_argument('--sync-status-file', type=Path)
    args = p.parse_args()
    os.nice(10)
    store = ReviewStore(args.root, args.cache, args.logs, not args.no_video_probe)
    store.sync_status_file = args.sync_status_file
    stopped = threading.Event()
    def worker():
        while not stopped.is_set():
            try: store.refresh()
            except Exception as exc:
                with store.lock: store.error = str(exc)
                print('ANALYSIS_RETRY ' + str(exc), flush=True)
            stopped.wait(5)
    threading.Thread(target=worker, name='ReadOnlyEpisodeAnalysis', daemon=True).start()
    token = None
    if args.auth_token_file:
        st = args.auth_token_file.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('token file must be owned by this user with mode 600')
        token = args.auth_token_file.read_text().strip()
        if len(token) < 32: raise ValueError('token too short')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(store, args.dist, token))
    print(f'PICO_REVIEW_READY http://127.0.0.1:{args.port} dataset={args.root}', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        stopped.set()
        server.server_close()


if __name__ == '__main__': main()
