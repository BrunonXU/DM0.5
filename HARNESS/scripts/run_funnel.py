"""Summarize S0–S9 from archived owner and planner traces without importing the runtime."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
from statistics import median


STAGES = dict(enumerate((
    "authorized", "first_publication", "approach_grasp", "grasp", "verify_grasp",
    "transfer_above_hole", "align", "insert", "verify_placed", "withdraw_reset",
)))
STAGE_KEYS = {skill: f"S{i}" for i, skill in STAGES.items()}
CLEANUP = {"session_closed", "task_finished", "owner_bridge_closed"}
USER_STOP = "web_stop_requested"
REASONS = Path(__file__).with_name("funnel_reasons.json")
REGISTRY = Path(__file__).resolve().parents[1] / "releases/registry.json"


def read_rows(directory, alternatives, label, issues):
    path = next((directory / name for name in alternatives if (directory / name).is_file()), None)
    if path is None:
        issues.append(f"{label}_missing")
        return None
    rows = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get("kind"), str):
                        raise ValueError("missing event kind")
                    rows.append(row)
    except (OSError, ValueError):
        issues.append(f"{label}_unreadable_line_{len(rows) + 1}")
        return None
    if not rows:
        issues.append(f"{label}_empty")
        return None
    return rows


def seconds(start, end):
    if type(start) is int and type(end) is int and end >= start:
        return round((end - start) / 1e9, 9)
    return None


def reason_code(value):
    """Keep stable reason codes, dropping dynamic free text, paths and measurements."""
    if not isinstance(value, str):
        return None
    tokens = []
    for part in value.split(":"):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,95}", part):
            break
        tokens.append(part)
    return ":".join(tokens) or "unstructured_reason"


def one(rows, kind):
    matches = [row for row in rows if row["kind"] == kind]
    return matches[0] if len(matches) == 1 else {}


def last_planner_failure(rows):
    candidate = None
    for index, row in enumerate(rows):
        kind = row["kind"]
        if kind in {"task_needs_attention", "planner_no_progress", "observation_readiness_timeout",
                    "planner_cancelled", "planner_authority_mismatch"}:
            candidate = (index, kind, reason_code(row.get("reason")) or kind, row.get("request_id"))
        elif kind == "model_rejected":
            status = row.get("http_status")
            reason = f"model_http_{status}" if type(status) is int else kind
            candidate = (index, kind, reason, row.get("request_id"))
        elif kind == "model_transport_error" and not row.get("automatic_retry"):
            candidate = (index, kind, reason_code(row.get("code") or row.get("error_type")) or kind,
                         row.get("request_id"))
        elif kind == "model_response" and candidate and candidate[1] in {"model_transport_error", "model_rejected"}:
            candidate = None
        elif kind == "owner_event" and (row.get("record") or {}).get("kind") == "skill_result":
            if (row["record"].get("result") or {}).get("outcome") == "success":
                candidate = None
    return candidate


def end_reason(planner, fault, skill):
    # A mirrored owner event can precede another planner event on the planner
    # clock. The reverse order may only reflect a delayed mirror.
    last = last_planner_failure(planner)
    planner_reason = last[2] if last else None
    if fault:
        return "authority_revoked", fault, planner_reason
    if not last:
        return ("skill_result" if skill else None), (skill["reason"] if skill else None), None
    if not skill or skill["reason"] == planner_reason:
        return last[1], planner_reason, planner_reason
    for event in planner[:last[0]]:
        if event["kind"] == "owner_event":
            owner = event.get("record") or {}
            if (owner.get("kind") == "skill_result" and skill["event_id"]
                    and owner.get("event_id") == skill["event_id"]):
                return last[1], planner_reason, planner_reason
    return "cross_clock_order_unknown", None, planner_reason


def model_metrics(rows):
    result = dict(model_calls=None, model_responses=None, model_transport_attempts_recorded=None,
                  model_requests_with_origin_evidence=None, model_transport_errors=None,
                  model_wait_s=None, model_wait_observed_s=None, model_requests_missing_wait=None)
    if rows is None:
        return result
    starts = {}
    ends = {}
    origin = set()
    attempts = set()
    errors = responses = 0
    consistent_ids = True
    for row in rows:
        kind = row["kind"]
        request = row.get("request_id")
        if kind == "model_request_started":
            if not request or request in starts:
                consistent_ids = False
            starts[request] = row.get("monotonic_ns")
        elif kind in {"model_transport_error", "model_transport_timing"}:
            errors += kind == "model_transport_error"
            attempts.add((request, row.get("attempt", row.get("event_id"))))
            if request and (row.get("origin_write_started") is True or kind == "model_transport_timing"):
                origin.add(request)
        if kind in {"model_response", "model_rejected"}:
            responses += kind == "model_response"
            if request:
                origin.add(request)
        if kind in {"model_response", "model_rejected"} or (
                kind == "model_transport_error" and not row.get("automatic_retry")):
            if request:
                end = row.get("elapsed_s")
                if type(end) not in (int, float) or not math.isfinite(end) or end < 0:
                    end = seconds(starts.get(request), row.get("monotonic_ns"))
                ends[request] = end
            else:
                # Early transports logged errors without request identity or start.
                consistent_ids = False
    consistent_ids = consistent_ids and not (set(ends) - set(starts)) and not (origin - set(starts))
    observed = [ends[req] for req in starts if ends.get(req) is not None]
    missing = len(starts) - len(observed)
    total = round(sum(observed), 9)
    result.update(model_calls=len(starts) if consistent_ids else None, model_responses=responses,
                  model_transport_attempts_recorded=len(attempts), model_transport_errors=errors,
                  model_requests_with_origin_evidence=len(origin), model_wait_observed_s=total,
                  model_requests_missing_wait=missing if consistent_ids else None,
                  model_wait_s=total if not missing and consistent_ids else None)
    return result


def identify(owner, planner, issues):
    prepared = one(owner, "task_prepared")
    auths = [r for r in owner if r["kind"] in {"web_authorization", "local_authorization"}]
    auth = auths[0] if len(auths) == 1 else {}
    identity = one(owner, "run_identity")
    remote = one(planner, "remote_task_prepared")
    if identity.get("synthetic") is True or auth.get("synthetic") is True:
        issues.append("synthetic")
    elif identity.get("synthetic") is not False or (auth and auth.get("synthetic") is not False):
        issues.append("physical_identity_unconfirmed")
    if not auth:
        issues.append("authorization_unconfirmed")
    if not prepared or len(auths) > 1 or not remote:
        issues.append("task_scope_unconfirmed")
    else:
        task_id = prepared.get("task", {}).get("task_id")
        session = prepared.get("session_id")
        if (not task_id or not session or remote.get("session_id") != session
                or remote.get("task", {}).get("task_id") != task_id
                or (auth and (auth.get("session_id") != session or auth.get("task_id") != task_id
                              or not prepared.get("task_hash") or auth.get("task_hash") != prepared["task_hash"]))):
            issues.append("task_scope_mismatch")
    if prepared.get("task", {}).get("goal") != "placed":
        issues.append("placement_task_unconfirmed")
    return prepared, auth


def owner_metrics(rows, auth, planner, table, ended):
    stamps = {key: None for key in STAGE_KEYS.values()}
    reached = {key: False for key in stamps}
    stage_request_s = {key: None for key in stamps}
    closure = next((i for i, r in enumerate(rows)
                    if r["kind"] == "authority_revoked" and r.get("reason") in CLEANUP), len(rows))
    proofs = {r.get("request_id") for r in rows[:closure] if r["kind"] == "owner_placement_verified"}
    proofs.discard(None)
    accepted = {r["intent"].get("request_id"): r["monotonic_ns"] for r in rows
                if r["kind"] == "skill_accepted" and isinstance(r.get("intent"), dict)
                and type(r.get("monotonic_ns")) is int}
    complete_assistance = True
    intervention_count = sum(r["kind"] == "manual_intervention" for r in planner)
    assisted_results = 0
    failed_reasons = Counter()
    nonphysical = Counter()
    physical = Counter()
    revoked = Counter()
    active_faults = []
    last_failed_result = None
    last_fault = None
    active = False
    # The first task closure ends the study window. Subsequent releases of the
    # already-closed owner remain visible in the raw revocation diagnostics.
    window = True
    incomplete_stages = set()
    for row in rows:
        kind = row["kind"]
        if kind == "authority_revoked":
            reason = reason_code(row.get("reason")) or "unknown"
            revoked[reason] += 1
            if active and reason not in CLEANUP | {USER_STOP}:
                active_faults.append(reason)
                last_fault = reason
            active = False
            if reason in CLEANUP:
                window = False
        if not window:
            continue
        if row is auth:
            active = True
            reached["S0"] = True
            stamps["S0"] = auth.get("monotonic_ns")
        elif kind == "manual_intervention":
            intervention_count += 1
        elif kind == "harness_skill_first_publication" and not reached["S1"] and active:
            reached["S1"] = True
            stamps["S1"] = row.get("published_ns")
        elif kind == "skill_result":
            res = row.get("result") or {}
            if res.get("assisted") is True:
                assisted_results += 1
            if type(res.get("assisted")) is not bool:
                complete_assistance = False
            request = res.get("request_id")
            stage = STAGE_KEYS.get(row.get("skill"))
            if res.get("outcome") == "success":
                last_failed_result = None
                if stage and active and (type(res.get("synthetic")) is not bool
                                           or (stage == "S8" and not request)):
                    incomplete_stages.add(stage)
                if (active and stage and not reached[stage] and res.get("synthetic") is False
                        and (stage != "S8" or request in proofs)):
                    reached[stage] = True
                    stamps[stage] = row.get("monotonic_ns")
                    stage_request_s[stage] = seconds(accepted.get(request), stamps[stage])
            elif res.get("outcome") in {"failure", "pending_review", "interrupted"}:
                raw = res.get("reason")
                reason = reason_code(raw) or "unknown"
                last_failed_result = dict(reason=reason, event_id=row.get("event_id"), request_id=request,
                                          nonphysical=False)
                if res["outcome"] == "failure":
                    failed_reasons[reason] += 1
                    if res.get("no_motion") is True:
                        is_physical = isinstance(raw, str) and (raw in table["physical_exact"]
                            or any(raw.startswith(prefix) for prefix in table["physical_prefix"]))
                        (physical if is_physical else nonphysical)[reason] += 1
                        last_failed_result["nonphysical"] = not is_physical
    reached = {key: value if value or (ended and key not in incomplete_stages) else None
               for key, value in reached.items()}
    verdict = False if reached["S8"] is False or intervention_count or assisted_results else None
    if reached["S8"] and not (assisted_results or intervention_count):
        verdict = True if complete_assistance and ended else None
    source, reason, planner_reason = end_reason(planner, last_fault, last_failed_result)
    if reached["S8"]:
        source, reason = None, None
    stage_s = {"S0": 0.0 if reached["S0"] else None}
    for i in range(1, 10):
        stage_s[f"S{i}"] = seconds(stamps[f"S{i-1}"], stamps[f"S{i}"])
    guard_termination = None
    if reached["S8"]:
        guard_termination = False
    elif ended and reason:
        guard_termination = False
        if last_failed_result and reason == last_failed_result["reason"] and last_failed_result["nonphysical"]:
            terminal = last_planner_failure(planner)
            if source == "skill_result" or (source == "authority_revoked" and last_fault == reason):
                guard_termination = True
            elif terminal and terminal[3] and terminal[3] == last_failed_result["request_id"]:
                guard_termination = True
            else:
                guard_termination = None
    return dict(stages=reached, stage_interval_s=stage_s, stage_skill_duration_s=stage_request_s,
                furthest_stage=next((s for s in reversed(reached) if reached[s]), None),
                complete_success=verdict, first_motion_s=seconds(stamps["S0"], stamps["S1"]),
                task_s=seconds(stamps["S0"], stamps["S8"]), reset_after_placement_s=stage_s["S9"],
                assisted_results=assisted_results, manual_interventions=intervention_count,
                physical_rejections=sum(physical.values()), nonphysical_rejections=sum(nonphysical.values()),
                rejection_reasons=dict(nonphysical=nonphysical, physical=physical),
                failed_skill_reasons=failed_reasons,
                revocations=sum(revoked.values()) - revoked[USER_STOP],
                revocations_raw=sum(revoked.values()), revocation_reasons=revoked,
                active_fault_revocations=len(active_faults), last_failure_source=source, last_failure_reason=reason,
                owner_last_failure_reason=last_fault or (last_failed_result["reason"] if last_failed_result else None),
                planner_last_failure_reason=planner_reason,
                nonphysical_termination=guard_termination if ended else None)


def analyze_run(directory, table, versions):
    issues = []
    owner = read_rows(directory, ("collected/owner/harness/trace.jsonl", "owner-trace.jsonl"), "owner", issues)
    planner = read_rows(directory, ("planner/trace.jsonl", "planner-trace.jsonl"), "planner", issues)
    config = {}
    config_path = directory / "workspace-run.json"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config = data
        except (OSError, ValueError):
            pass
    settings = config.get("config") or {}
    model = model_metrics(planner)
    if owner is not None and planner is not None:
        _, auth = identify(owner, planner, issues)
    else:
        auth = {}
    bundle = one(owner or [], "development_admission").get("bundle_id")
    scenario = one(owner or [], "run_identity").get("scenario_id")
    status = config.get("status")
    ended = any(r["kind"] == "authority_revoked" and r.get("reason") in CLEANUP for r in owner or [])
    metrics = owner_metrics(owner or [], auth, planner or [], table, ended)
    if issues:
        metrics = {key: None for key in metrics}
        metrics["stages"] = {s: None for s in STAGE_KEYS.values()}
    return dict(run_id=directory.name, status=issues[0] if issues else "included", eligible=not issues,
                issues=issues, workspace_status=status, started_at=config.get("started_at"),
                mode=settings.get("mode") if isinstance(settings, dict) else None,
                bundle_id=bundle, version=versions.get(bundle), scenario_id=scenario, ended=ended,
                planner_mirror_incomplete=any(e["kind"] == "owner_evidence_export_incomplete"
                                              for e in planner or []) if planner is not None else None,
                **metrics, **model)


def metric(rows, key, aggregate):
    values = [r[key] for r in rows if r[key] is not None]
    return dict(value=round(aggregate(values), 9) if values else None, n=len(values))


def summarize(runs):
    rows = [r for r in runs if r["eligible"]]
    reasons = Counter()
    for row in rows:
        reasons.update(row["failed_skill_reasons"])
    total = len(rows)
    stages = {}
    for stage in STAGE_KEYS.values():
        n = sum(r["stages"][stage] is True for r in rows)
        intervals = [r["stage_interval_s"][stage] for r in rows if r["stage_interval_s"][stage] is not None]
        known = sum(r["stages"][stage] is not None for r in rows)
        stages[stage] = dict(reached=n, trials=known, unknown=total-known, rate=n / known if known else None,
                             observed_lower_bound=n/total if total else None,
                             interval_median_s=median(intervals) if intervals else None, interval_n=len(intervals))
    end_reasons = Counter(r["last_failure_reason"] for r in rows if r["last_failure_reason"])
    return dict(directories=len(runs), included=total, status_counts=Counter(r["status"] for r in runs),
                issue_counts=Counter(issue for r in runs for issue in r["issues"]),
                mode_counts=Counter(r["mode"] or "unknown" for r in rows),
                version_counts=Counter(str(r["version"]) if r["version"] is not None else "unknown" for r in rows),
                stages=stages, complete_successes=sum(r["complete_success"] is True for r in rows),
                success_unknown=sum(r["complete_success"] is None for r in rows),
                success_rate=metric(rows, "complete_success", lambda values: sum(values)/len(values)),
                first_motion_median_s=metric(rows, "first_motion_s", median),
                task_median_s=metric(rows, "task_s", median), model_calls_median=metric(rows, "model_calls", median),
                model_wait_median_s=metric(rows, "model_wait_s", median),
                nonphysical_rejections_mean=metric(rows, "nonphysical_rejections", lambda v: sum(v)/len(v)),
                nonphysical_rejections=sum(r["nonphysical_rejections"] for r in rows),
                physical_rejections=sum(r["physical_rejections"] for r in rows),
                nonphysical_termination_rate=metric(rows, "nonphysical_termination", lambda v: sum(v)/len(v)),
                closed_owner_traces=sum(r["ended"] for r in rows),
                closed_nonphysical_rejections_mean=metric([r for r in rows if r["ended"]],
                    "nonphysical_rejections", lambda v: sum(v)/len(v)),
                revocations=sum(r["revocations"] for r in rows),
                revocations_raw=sum(r["revocations_raw"] for r in rows),
                active_fault_revocations=sum(r["active_fault_revocations"] for r in rows),
                failed_skill_reasons=dict(sorted(reasons.items(), key=lambda i: (-i[1], i[0]))),
                last_failure_reasons=dict(sorted(end_reasons.items(), key=lambda i: (-i[1], i[0]))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="*", type=Path, help="Individual run or exported evidence directories")
    parser.add_argument("--workspace", type=Path, help="Inspect every immediate run directory in this workspace")
    parser.add_argument("--output", type=Path, help="Write the report here (default: stdout)")
    args = parser.parse_args()
    paths = list(args.runs)
    if args.workspace:
        paths.extend(p for p in args.workspace.iterdir() if p.is_dir())
    if not paths:
        parser.error("provide run directories or --workspace")
    paths = sorted({p.resolve() for p in paths}, key=lambda p: (p.name, str(p)))
    if len({p.name for p in paths}) != len(paths):
        parser.error("run directory names must identify distinct runs")
    if args.output and (any(args.output.resolve().is_relative_to(p) for p in paths)
                        or args.workspace and args.output.resolve().is_relative_to(args.workspace.resolve())):
        parser.error("--output must be outside the input runs and workspace")
    table = json.loads(REASONS.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    versions = {r["bundle_id"]: r["version"] for r in registry["releases"] if r.get("bundle_id")}
    runs = [analyze_run(p, table, versions) for p in paths]
    report = dict(schema="f1-harness-funnel-v1", runs=runs, summary=summarize(runs))
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
