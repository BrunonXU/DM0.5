#!/usr/bin/env python3
"""Read the local agent briefing, or explicitly refresh the document index."""
import argparse
import json
import sys

from hub import HERE, Hub, HubError
from review_store import ReviewStore


def main():
    parser = argparse.ArgumentParser(description="Projects Hub 文档索引工具")
    parser.add_argument("command", choices=["briefing", "scan", "state"])
    parser.add_argument("--workspace", default=str(HERE.parent))
    parser.add_argument("--runtime", default=str(HERE / "runtime"))
    parser.add_argument("--config", default=str(HERE / "config/projects.json"))
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    try:
        hub = Hub(args.workspace, args.runtime, args.config)
        result = getattr(hub, args.command)()
        if args.command in {'briefing', 'state'}:
            result['whereweare'] = ReviewStore(hub).state()
        print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    except (HubError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
