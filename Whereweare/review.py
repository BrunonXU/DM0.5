#!/usr/bin/env python3
"""Prepare a local evidence packet and publish a reviewed Whereweare candidate."""
import argparse
import json
from pathlib import Path
import sys

from hub import HERE, Hub, HubError
from review_store import ReviewStore


def main():
    parser = argparse.ArgumentParser(description="Whereweare 文档审阅与发布")
    parser.add_argument("--workspace", default=str(HERE.parent))
    parser.add_argument("--runtime", default=str(HERE / "runtime"))
    parser.add_argument("--config", default=str(HERE / "config/projects.json"))
    parser.add_argument("--tracking-root", help="覆盖精选摘要目录；隔离 runtime 默认在自身 tracking 下保存")
    parser.add_argument("--compact", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="扫描已登记文档并形成待审阅证据包")
    prepare.add_argument("--mode", choices=["update", "daily", "weekly", "baseline"], default="update")
    source = commands.add_parser('read-source', help='按行补读已准备证据包中的文档')
    source.add_argument('--packet', required=True)
    source.add_argument('--path', required=True)
    source.add_argument('--start', type=int, default=1)
    source.add_argument('--end', type=int)
    publish = commands.add_parser("publish", help="校验证据、保存历史并原子发布候选摘要")
    publish.add_argument("--packet", required=True)
    publish.add_argument("--candidate", required=True)
    commands.add_parser("status", help="查看最新精选摘要与来源新鲜度")
    args = parser.parse_args()
    try:
        hub = Hub(args.workspace, args.runtime, args.config)
        store = ReviewStore(hub, args.tracking_root)
        if args.command == "prepare":
            result = store.prepare_report(args.mode) if args.mode in {'daily', 'weekly'} else store.prepare(args.mode)
        elif args.command == 'read-source':
            result = store.read_source(args.packet, args.path, args.start, args.end)
        elif args.command == "publish":
            candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
            result = store.publish_report(candidate, args.packet) if store._packet(args.packet).get('kind') == 'report' else store.publish(candidate, args.packet)
        else:
            result = store.state()
        print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    except (HubError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
