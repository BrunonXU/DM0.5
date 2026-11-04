#!/usr/bin/env python3
"""Read-only local teaching library. Never imports robot or training code."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import re
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import markdown

APP = Path(__file__).resolve().parent
PROJECTS = Path("/home/user/projects").resolve()
WHYHOW_SOURCE = Path("/home/user/Tools/WhyHow").resolve()
DOCUMENT_ROOTS = [
    PROJECTS / "tasks/F1_ComplexyToys",
    PROJECTS / "HARNESS/docs",
    WHYHOW_SOURCE / "content",
]
RESOURCE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".csv",
                  ".json", ".txt", ".py", ".sh", ".yaml", ".yml"}


def within(path: Path, roots) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots)


def document_label(path: Path) -> Path:
    """Keep reader URLs stable when the teaching source lives outside Projects."""
    if path.is_relative_to(PROJECTS):
        return path.relative_to(PROJECTS)
    return Path("WhyHow") / path.relative_to(WHYHOW_SOURCE)


def slug(value: str, separator: str) -> str:
    value = re.sub(r"[^\w\s-]", "", value.lower(), flags=re.UNICODE)
    return re.sub(r"[\s-]+", separator, value).strip(separator)


def flatten_toc(nodes):
    result = []
    for node in nodes:
        result.append(dict(id=node["id"], title=html.unescape(re.sub("<[^>]*>", "", node["name"])),
                           level=node["level"]))
        result.extend(flatten_toc(node.get("children", [])))
    return result


def themed_interactive(source):
    """Theme registered lessons at serving time, without changing their original files."""
    html = source.read_text(encoding="utf-8")
    link = '<link rel="stylesheet" href="/web/interactive.css">'
    return re.sub(r"</head\s*>", lambda match: link + "\n" + match.group(), html,
                  count=1, flags=re.IGNORECASE).encode("utf-8")


class SafeHTML(HTMLParser):
    """Allow semantic document markup and rewrite only explicitly allowed files."""
    tags = set("h1 h2 h3 h4 h5 h6 p pre code strong em a ul ol li blockquote table "
               "thead tbody tr th td hr br img div span details summary del s sup sub input".split())
    void = {"hr", "br", "img", "input"}
    forbidden = {"script", "style", "iframe", "object", "embed"}

    def __init__(self, source: Path, library):
        super().__init__(convert_charrefs=True)
        self.source, self.library, self.output, self.suppressed = source, library, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.forbidden:
            self.suppressed += 1
            return
        if self.suppressed or tag not in self.tags:
            return
        clean = []
        for key, value in attrs:
            if value is None:
                if key in {"open", "disabled", "checked"}:
                    clean.append((key, key))
                continue
            if key in {"id", "class", "title", "alt", "colspan", "rowspan", "start"}:
                clean.append((key, value))
            elif tag == "input" and key == "type" and value == "checkbox":
                clean.append((key, value))
            elif key in {"href", "src"} and ((tag == "a" and key == "href") or (tag == "img" and key == "src")):
                rewritten = self.library.link(self.source, value, image=(tag == "img"))
                if rewritten:
                    clean.append((key, rewritten))
        if tag == "a":
            url = next((value for key, value in clean if key == "href"), "")
            if url.startswith(("https://", "http://")):
                clean += [("target", "_blank"), ("rel", "noopener noreferrer")]
        if tag == "input":
            clean.append(("disabled", "disabled"))
        attributes = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in clean)
        self.output.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag):
        if tag in self.forbidden:
            self.suppressed = max(0, self.suppressed - 1)
        elif not self.suppressed and tag in self.tags and tag not in self.void:
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.suppressed:
            self.output.append(html.escape(data, quote=False))


class Library:
    def __init__(self, catalog_path=APP / "catalog.json"):
        self.catalog_path = Path(catalog_path)
        self.resources = {}

    def catalog(self):
        data = json.loads(self.catalog_path.read_text())
        items = []
        for item in data["items"]:
            path = Path(item["source"]).resolve()
            if not within(path, DOCUMENT_ROOTS) or not path.is_file():
                continue
            items.append({**{k: item[k] for k in ("id", "kind", "title", "description", "category")},
                          "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
                          "source_label": str(document_label(path))})
        return {"service": {"name": "Why&How", "port": 4243}, "items": items}

    def item(self, ident):
        for item in json.loads(self.catalog_path.read_text())["items"]:
            if item["id"] == ident:
                path = Path(item["source"]).resolve()
                if not path.is_file() or not within(path, DOCUMENT_ROOTS):
                    raise FileNotFoundError("文档尚未就绪")
                return item, path
        raise FileNotFoundError("没有这个阅读条目")

    def document_path(self, relative):
        # API accepts only non-hidden Markdown files in the selected task/document roots.
        relative = Path(relative)
        if relative.parts[:1] == ("WhyHow",):
            path = (WHYHOW_SOURCE / Path(*relative.parts[1:])).resolve()
        else:
            path = (PROJECTS / relative).resolve()
        if (not within(path, DOCUMENT_ROOTS) or path.suffix.lower() != ".md"
                or any(part.startswith(".") for part in document_label(path).parts)
                or not path.is_file()):
            raise FileNotFoundError("这份文件没有纳入阅读范围")
        return path

    def link(self, source: Path, original: str, image=False):
        parsed = urlsplit(original)
        if parsed.scheme in {"http", "https"}:
            return original if not image else ""  # Reader works without third-party tracking images.
        if parsed.scheme or parsed.netloc:
            return ""
        if not parsed.path:
            return "#" + quote(unquote(parsed.fragment), safe="-_") if parsed.fragment else ""
        raw = re.sub(r":\d+$", "", unquote(parsed.path))
        path = (Path(raw) if raw.startswith("/") else source.parent / raw).resolve()
        if not within(path, DOCUMENT_ROOTS) or not path.is_file():
            return ""
        if path.suffix.lower() == ".md" and not image:
            query = "path=" + quote(str(document_label(path)), safe="")
            if parsed.fragment:
                query += "&anchor=" + quote(unquote(parsed.fragment), safe="")
            return "/?" + query
        if path.suffix.lower() == ".html" and not image:
            for item in json.loads(self.catalog_path.read_text())["items"]:
                if item["kind"] == "html" and Path(item["source"]).resolve() == path:
                    return "/?html=" + quote(item["id"], safe="")
            return ""
        if path.suffix.lower() not in RESOURCE_TYPES:
            return ""
        ident = hashlib.sha256(str(path).encode()).hexdigest()[:32]
        self.resources[ident] = path
        return f"/resource/{ident}/{quote(path.name)}"

    def document(self, ident=None, relative=None):
        if ident is not None:
            item, path = self.item(ident)
            if item["kind"] != "markdown":
                raise FileNotFoundError("此条目是交互页面")
            title = item["title"]
        else:
            path = self.document_path(relative or "")
            title = path.stem
        raw = path.read_text()
        # Task-list semantics without requiring a second Markdown parser.
        raw = re.sub(r"(?m)^(\s*[-*]\s+)\[([ xX])\]\s",
                     lambda m: m[1] + '<input type="checkbox" disabled' +
                     (' checked' if m[2].lower() == "x" else '') + '> ', raw)
        md = markdown.Markdown(extensions=["extra", "toc", "sane_lists"],
                               extension_configs={"toc": {"slugify": slug}})
        rendered = md.convert(raw)
        clean = SafeHTML(path, self)
        clean.feed(rendered)
        clean.close()
        if ident is None and md.toc_tokens:
            title = md.toc_tokens[0]["name"]
        return dict(title=title, html="".join(clean.output), toc=flatten_toc(md.toc_tokens),
                    source_label=str(document_label(path)),
                    updated_at=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat())


class Handler(BaseHTTPRequestHandler):
    server_version = "WhyHow/1"

    def respond(self, body, mime, status=200, attachment=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if attachment:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(attachment))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, value, status=200):
        self.respond(json.dumps(value, ensure_ascii=False), "application/json; charset=utf-8", status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        library = self.server.library
        try:
            if path == "/api/health":
                return self.json(dict(ok=True, service="Why&How", port=self.server.server_port,
                                      readonly=True, items=len(library.catalog()["items"])))
            if path == "/api/catalog":
                return self.json(library.catalog())
            if path.startswith("/api/document/"):
                return self.json(library.document(ident=path.removeprefix("/api/document/")))
            if path == "/api/document":
                return self.json(library.document(relative=query.get("path", [""])[0]))
            if path.startswith("/interactive/"):
                ident = path.removeprefix("/interactive/").strip("/")
                item, source = library.item(ident)
                if item["kind"] != "html":
                    raise FileNotFoundError("不是交互页面")
                return self.respond(themed_interactive(source), "text/html; charset=utf-8")
            if path.startswith("/resource/"):
                ident = path.split("/")[2]
                source = library.resources.get(ident)
                if source is None or not source.is_file() or not within(source, DOCUMENT_ROOTS):
                    raise FileNotFoundError("请先打开引用这份附件的文章")
                suffix = source.suffix.lower()
                mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                inline = suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}
                return self.respond(source.read_bytes(), mime,
                                    attachment=None if inline else source.name)
            if path in {"/", "/index.html"}:
                return self.respond((APP / "web/index.html").read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/web/"):
                source = (APP / path.removeprefix("/")).resolve()
                if not within(source, [APP / "web"]) or not source.is_file():
                    raise FileNotFoundError("资源不存在")
                mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                return self.respond(source.read_bytes(), mime)
            raise FileNotFoundError("页面不存在")
        except (FileNotFoundError, ValueError, KeyError):
            return self.json({"error": "内容不存在或未纳入本站。请从左侧目录打开。"}, 404)
        except (OSError, json.JSONDecodeError):
            self.log_error("content read failed")
            return self.json({"error": "内容暂时无法读取，请刷新重试。"}, 503)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4243)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.library = Library()
    print(f"Why&How listening on {args.host}:{args.port}; read-only", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
