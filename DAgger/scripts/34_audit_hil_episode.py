#!/usr/bin/env python3
"""Audit a finalized HIL episode journal; never imports GDK."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from g2dagger.hil_episode import audit_hil_episode
from g2dagger.recording_audit import audit_payload_archive, audit_journal_payload_links


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--payload-dir", type=Path)
    args = parser.parse_args()
    report = audit_hil_episode(args.episode_dir)
    if args.payload_dir is not None:
        report["payload_audit"] = audit_payload_archive(args.payload_dir)
        report["errors"].extend(audit_journal_payload_links(args.episode_dir, args.payload_dir))
        report["valid"] = report["valid"] and report["payload_audit"]["valid"] and not report["errors"]
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(args.output)
    print(payload, end="")
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
