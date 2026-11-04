#!/usr/bin/env python3
"""Build a local structural Graphify graph, including Markdown headings/links.

The input should be a curated source snapshot. No application code is imported,
no model backend is called, and no assistant settings are installed.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from graphify.extract import extract, _DISPATCH
from graphify.build import build_from_json
from graphify.export import to_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    supported = [p for p in files if p.suffix.lower() in _DISPATCH]
    start = time.monotonic()
    result = extract(supported, root=root, cache_root=output, max_workers=2)
    graph_dir = output / "graphify-out"
    graph_dir.mkdir(exist_ok=True)
    (graph_dir / "extraction.json").write_text(json.dumps(result, ensure_ascii=False))
    graph = build_from_json(result, root=root)
    graph.graph["pilot_extraction_mode"] = "local structural extraction; no semantic LLM pass"
    to_json(graph, {}, str(graph_dir / "graph.json"), original_links=result["edges"])
    (graph_dir / ".graphify_root").write_text(str(root))
    counts = Counter(n.get("source_file") for n in result["nodes"])
    coverage = {
        "created": datetime.now(timezone.utc).isoformat(),
        "mode": "Graphify native AST and Markdown structural extractors; no model backend",
        "source": str(root), "elapsed_seconds": time.monotonic() - start,
        "snapshot_files": len(files), "parser_inputs": len(supported),
        "nodes": len(graph), "edges": graph.number_of_edges(),
        "raw_nodes": len(result["nodes"]), "raw_edges": len(result["edges"]),
        "relations": dict(Counter(e.get("relation") for e in result["edges"])),
        "confidences": dict(Counter(e.get("confidence") for e in result["edges"])),
        "unsupported_files": [str(p.relative_to(root)) for p in files if p not in supported],
        "files": [{"path": str(p.relative_to(root)), "nodes": counts[str(p.relative_to(root))],
                   "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in supported],
    }
    (output / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in coverage.items() if k not in {"files", "unsupported_files"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
