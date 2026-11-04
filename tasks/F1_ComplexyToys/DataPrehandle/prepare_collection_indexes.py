#!/usr/bin/env python3
"""Freeze manifest-bound multi-episode temporal indexes and pairing diagnostics.

Read-only raw access; output is a new directory. The success confirmation is kept
separate from technical QC and is never converted into invented reviewed ranges.
This produces temporal indexes, not normalized model tensors or training approval.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

from build_training_index import build_index, load_config, sha256
from timing_audit import compare_grid, read_jsonl, stats


def validate_sources(catalog, confirmation):
    episodes = catalog["episodes"]
    identities = [r["run"] for r in episodes]
    if not identities or len(set(identities)) != len(identities):
        raise ValueError("Empty collection or duplicate episodes")
    for name in identities:
        if Path(name).name != name or name in (".", ".."):
            raise ValueError("Unsafe episode name")
    approved = {r["episode"]: r["manifest_sha256"] for r in confirmation["scope"]}
    expected = {r["run"]: r["manifest_sha256"] for r in episodes}
    if approved != expected or len(confirmation["scope"]) != len(approved):
        raise ValueError("Success confirmation must bind exactly this collection")
    return episodes


def run(catalog_path, confirmation_path, config_path, output, raw_root=None, home_review_path=None):
    catalog = json.loads(catalog_path.read_text())
    confirmation = json.loads(confirmation_path.read_text())
    episodes = validate_sources(catalog, confirmation)
    home_reviews = json.loads(home_review_path.read_text())["episodes"] if home_review_path else {}
    config, document = load_config(config_path)
    output = output.resolve()
    sources = [(raw_root / r["run"] if raw_root else Path(r["path"])).resolve() for r in episodes]
    if any(output == raw or raw in output.parents for raw in sources):
        raise ValueError("Output cannot be inside a source recording")
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise FileExistsError("Use a new output directory")
    # Bind all sources before publishing any candidate.
    for row, raw in zip(episodes, sources):
        if sha256(raw / "journal/manifest.json") != row["manifest_sha256"]:
            raise ValueError("Source identity changed: " + row["run"])
    partial.mkdir(parents=True)
    for source, name in ((catalog_path, "catalog.json"), (confirmation_path, "user-confirmation.json"),
                         (config_path, "preprocess.json")):
        shutil.copyfile(source, partial / name)
    if home_review_path:
        shutil.copyfile(home_review_path, partial / "home-review.json")
    reports, rejection_counts = [], Counter()
    for row, raw in zip(episodes, sources):
        name = row["run"]
        index_dir = partial / "episodes" / name
        report = build_index(raw, index_dir, mode="technical", config=config, config_document=document, home_review=home_reviews.get(name))
        anchors = read_jsonl(index_dir / "anchors.jsonl")
        if not anchors:
            raise ValueError("No surviving action windows: " + name)
        # Counterfactual only: online still consumes the original whole groups.
        grid = [dict(session_id=name, source_row=a["observation_id"],
                     source_observation_monotonic_ns=a["source_observation_monotonic_ns"],
                     time_ns=a["anchor_monotonic_ns"], segment_id=a["intervention_id"],
                     images={f["camera"]: f for f in a["frames"]}) for a in anchors]
        pairing = compare_grid(read_jsonl(raw / "payload/frames.jsonl"), grid, name)
        pairing.pop("selection_provenance")  # Existing anchor index retains baseline provenance.
        pairing["candidate_selection_rule"] = "latest saved host receipt <= unchanged anchor; diagnostic only"
        metrics = dict(
            observation_age_ms=stats(a["observation_age_ns"] / 1e6 for a in anchors),
            state_age_ms=stats(t / 1e6 for a in anchors for t in a["state_ages_ns"]),
            command_age_ms=stats(c["command_age_ns"] / 1e6 for a in anchors for c in a["action_window"]),
            reused_anchor_count=len(anchors) - report["summary"]["unique_anchor_observations"],
            pairing=pairing)
        (index_dir / "timing-quality.json").write_text(json.dumps(metrics, indent=2) + "\n")
        rejection_counts.update(report["summary"]["rejection_counts"])
        reports.append(dict(episode=name, source_manifest_sha256=row["manifest_sha256"],
                            summary=report["summary"], metrics=metrics,
                            files_sha256={p.name: sha256(p) for p in index_dir.iterdir() if p.is_file()}))
        print(json.dumps(dict(episode=name, windows=len(anchors), audit="PASS")), flush=True)
    totals = {key: sum(r["summary"][key] for r in reports) for key in
              ("grid_points", "retained_windows", "complete_action_windows", "partial_action_windows", "home_anchor_windows", "windows_containing_home", "unique_anchor_observations", "raw_observations")}
    totals.update(rejection_counts=dict(rejection_counts),
                  grid_retention=totals["retained_windows"] / totals["grid_points"],
                  unique_observation_retention=totals["unique_anchor_observations"] / totals["raw_observations"])
    result = dict(schema="f1_collection_time_indexes_v1", created_utc=datetime.now(timezone.utc).isoformat(),
                  training_ready=False, stage="audited_time_indexes_with_diagnostic_pairing_only",
                  user_success_confirmation_sha256=sha256(confirmation_path), config=asdict(config),
                  builder_sha256=sha256(Path(__file__)), episodes=reports, totals=totals,
                  remaining=["model dataset packaging and episode split", "train-only EEF statistics",
                             "image/reader validation", "H20 training preflight"],
                  limitations=["No calibrated exposure clock or fixed latency compensation",
                               "Independent-camera candidates are not used for labels/model inputs",
                               "Success confirmation does not certify every frame as an expert label"])
    (partial / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    os.replace(partial, output)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--confirmation", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--home-review", type=Path)
    args = parser.parse_args()
    result = run(args.catalog, args.confirmation, args.config, args.output, args.raw_root, args.home_review)
    print(json.dumps(result["totals"], ensure_ascii=False), flush=True)
