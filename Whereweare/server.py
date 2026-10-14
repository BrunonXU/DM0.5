#!/usr/bin/env python3
"""Small localhost HTTP server for Projects Hub."""
import argparse
import ipaddress
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from hub import HERE, Hub, HubError
from review_store import ReviewStore
from review_jobs import ReviewJobs
from memory import decisions, save_decision, action_key

MAX_BODY_BYTES = 65536


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, hub, static_dir=None):
        super().__init__(address, Handler)
        self.hub = hub
        self.reviews = ReviewStore(hub)
        self.review_jobs = ReviewJobs(hub, self.reviews)
        self.static_dir = Path(static_dir or HERE / "web").resolve()
        bind_host = address[0]
        try:
            self.local_only = ipaddress.ip_address(bind_host).is_loopback
        except ValueError:
            self.local_only = bind_host == "localhost"

    def review_state(self):
        result = {**self.reviews.state(), **self.review_jobs.state()}
        inventory = self.reviews.tracking_root / 'directories.json'
        result['directories'] = json.loads(inventory.read_text()).get('directories', []) if inventory.is_file() else []
        known = {item['path'].split('/')[0] for item in result['directories']}
        result['untracked_directories'] = sorted(path.name for path in self.hub.workspace.iterdir()
                                                if path.is_dir() and not path.name.startswith('.') and path.name not in known)
        return result

    def workspace_state(self, briefing=False):
        result = self.hub.briefing() if briefing else self.hub.state()
        preferences = self.hub.tracking_preferences()
        for project in result['projects']:
            project['tracking'] = preferences[project['id']]
        result['whereweare'] = self.review_state()
        result['whereweare']['decisions'] = decisions(self.hub)
        result['whereweare']['action_keys'] = {p['id']: action_key(p.get('next_step', '')) for p in (result['whereweare'].get('snapshot') or {}).get('projects', [])}
        if briefing:
            result['memory'] = {key: (result['whereweare'].get('snapshot') or {}).get(key) for key in ('revision', 'reviewed_at', 'projects', 'events')}
            result['memory']['decisions'] = result['whereweare']['decisions']
            result['memory']['freshness'] = result['whereweare']['freshness']
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = "ProjectsHub/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Never log request bodies or document query strings.
        print(f"{self.log_date_time_string()} {self.command} {urlsplit(self.path).path}", file=sys.stderr)

    def send(self, status, content, content_type="application/json; charset=utf-8"):
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        elif isinstance(content, str):
            content = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def check_host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            raise HubError("需要唯一有效的 Host", 403)
        host = hosts[0]
        try:
            parsed = urlsplit("http://" + host)
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError()
            port = parsed.port
        except ValueError:
            raise HubError("无效 Host", 403)
        if self.server.local_only and (parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or (port or 80) != self.server.server_port):
            raise HubError("仅允许本机访问", 403)
        return host

    def body(self):
        host = self.check_host()
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host, "https://" + host}:
            raise HubError("仅接受同源写入", 403)
        if self.headers.get("Sec-Fetch-Site") in {"cross-site"}:
            raise HubError("拒绝跨站写入", 403)
        if self.headers.get_content_type() != "application/json":
            raise HubError("写入需要 application/json", 415)
        if self.headers.get("Transfer-Encoding"):
            raise HubError("不接受分块请求", 400)
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError()
            length = int(lengths[0])
            if length < 0:
                raise ValueError()
        except ValueError:
            raise HubError("需要有效 Content-Length", 400)
        if length > MAX_BODY_BYTES:
            raise HubError("请求超过 64 KiB", 413)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError):
            raise HubError("无效 JSON", 400)
        if not isinstance(data, dict):
            raise HubError("JSON 必须为对象")
        return data

    def dispatch(self):
        self.check_host()
        parsed = urlsplit(self.path)
        path = parsed.path
        hub = self.server.hub
        if self.command in {"GET", "HEAD"}:
            if path == "/api/state":
                return self.send(200, self.server.workspace_state())
            if path == "/api/briefing":
                return self.send(200, self.server.workspace_state(briefing=True))
            if path == "/api/reports":
                query = parse_qs(parsed.query)
                return self.send(200, self.server.reviews.read_report(query.get("mode", ["daily"])[0], query.get("date", [None])[0]))
            if path == "/api/review/status":
                return self.send(200, self.server.review_state())
            if path == "/api/health":
                return self.send(200, {"ok": True, "mode": "local-documents"})
            if path == "/api/document":
                values = parse_qs(parsed.query).get("path", [])
                if len(values) != 1:
                    raise HubError("需要一个 path 参数")
                return self.send(200, hub.document(values[0]))
            if path.startswith("/api/"):
                raise HubError("接口不存在", 404)
            relative = unquote(path).lstrip("/") or "index.html"
            if "\x00" in relative or "\\" in relative or ".." in Path(relative).parts:
                raise HubError("无效资源路径", 403)
            file_path = self.server.static_dir / relative
            if not file_path.resolve().is_relative_to(self.server.static_dir) or file_path.is_symlink():
                raise HubError("资源路径超出范围", 403)
            if not file_path.is_file():
                raise HubError("资源不存在", 404)
            mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            if mime.startswith("text/") or mime == "application/javascript":
                mime += "; charset=utf-8"
            return self.send(200, file_path.read_bytes(), mime)
        data = self.body()
        if self.command == "POST":
            if path == "/api/review/automation":
                return self.send(200, self.server.review_jobs.configure_automation(data))
            if path == "/api/decisions":
                return self.send(200, save_decision(hub, self.server.reviews.state()["snapshot"], data))
            if path == '/api/review/run':
                if 'mode' not in data or set(data) - {'mode', 'date'}:
                    raise HubError('仅接受 mode、date 字段')
                return self.send(202, self.server.review_jobs.start(data['mode'], data.get('date')))
            routes = {"/api/scan": lambda: hub.scan(), "/api/tasks": lambda: hub.add_task(data),
                      "/api/timeline/nodes": lambda: hub.add_timeline_node(data),
                      "/api/timer/start": lambda: hub.start_timer(data), "/api/timer/stop": hub.stop_timer,
                      "/api/time": lambda: hub.add_time(data), "/api/projects": lambda: hub.add_project(data)}
            if path in routes:
                return self.send(201 if path in {"/api/tasks", "/api/time", "/api/projects", "/api/timeline/nodes"} else 200, routes[path]())
        if self.command == 'PATCH' and path.startswith('/api/projects/') and path.endswith('/tracking'):
            project_id = path[len('/api/projects/'):-len('/tracking')]
            if not project_id or '/' in project_id:
                raise HubError('无效项目 ID')
            return self.send(200, hub.update_tracking(project_id, data))
        if self.command == "PATCH" and path.startswith("/api/timeline/nodes/"):
            node_id = path.removeprefix("/api/timeline/nodes/")
            if not node_id or "/" in node_id or len(node_id) > 100:
                raise HubError("无效节点 ID")
            return self.send(200, hub.update_timeline_node(node_id, data))
        if self.command == "PATCH" and path.startswith("/api/tasks/"):
            task_id = path.removeprefix("/api/tasks/")
            if not task_id or "/" in task_id or len(task_id) > 100:
                raise HubError("无效任务 ID")
            return self.send(200, hub.update_task(task_id, data))
        raise HubError("接口不存在", 404)

    def handle_request(self):
        self.connection.settimeout(15)
        try:
            self.dispatch()
        except HubError as error:
            self.close_connection = True
            self.send(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (FileNotFoundError, PermissionError, UnicodeError) as error:
            self.close_connection = True
            self.send(404 if isinstance(error, FileNotFoundError) else 400, {"error": "文档不可读取，请检查来源并重新同步"})
        except Exception as error:
            self.close_connection = True
            print(f"Projects Hub request failed: {type(error).__name__}", file=sys.stderr)
            self.send(500, {"error": "操作失败，请检查服务日志与同步状态"})

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PATCH = handle_request


def main():
    parser = argparse.ArgumentParser(description="Projects Hub 本地文档中台")
    parser.add_argument("--workspace", default=str(HERE.parent))
    parser.add_argument("--runtime", default=str(HERE / "runtime"))
    parser.add_argument("--config", default=str(HERE / "config/projects.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4343)
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    hub = Hub(args.workspace, args.runtime, args.config, args.interval)
    hub.scan()
    server = HubServer((args.host, args.port), hub)
    server.timeout = 0.5
    hub.start_watcher()
    def stop(signum, frame):
        hub.stop_event.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"Projects Hub: http://{args.host}:{server.server_port} · workspace={hub.workspace} · runtime={hub.runtime}", flush=True)
    if not server.local_only:
        print("Explicit non-loopback bind enabled; this server has no authentication. Use a trusted network or authenticated reverse proxy.", file=sys.stderr)
    try:
        while not hub.stop_event.is_set():
            server.handle_request()
            try:
                server.review_jobs.tick()
            except Exception as error:
                print(f"Auto review deferred: {type(error).__name__}", file=sys.stderr)
    finally:
        hub.stop_event.set()
        server.review_jobs.close()
        server.server_close()


if __name__ == "__main__":
    main()
