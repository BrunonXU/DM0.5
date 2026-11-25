"""Archived real/synthetic, clock, retry and incomplete-record edges in the offline funnel."""
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_funnel.py"
spec = importlib.util.spec_from_file_location("run_funnel", SCRIPT)
funnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(funnel)
TABLE = {"physical_exact": ["collision"], "physical_prefix": []}


def row(kind, index, **kwargs):
    return dict(kind=kind, event_id=f"e{index}", monotonic_ns=index * 1_000_000_000, **kwargs)


def skill(index, name, rid, **kwargs):
    result = dict(request_id=rid, outcome="success", synthetic=False, assisted=False)
    result.update(kwargs)
    return row("skill_result", index, skill=name, result=result)


def base():
    task = dict(task_id="trial", goal="placed")
    owner = [row("run_identity", 1, synthetic=False),
             row("task_prepared", 2, session_id="A", task_hash="hash", task=task),
             row("web_authorization", 3, session_id="A", task_hash="hash", task_id="trial", synthetic=False)]
    return owner, [row("remote_task_prepared", 1000, session_id="A", task=task)]


def analyze(directory, owner, planner):
    for path, events in [(directory / "owner-trace.jsonl", owner), (directory / "planner-trace.jsonl", planner)]:
        if events is not None:
            path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return funnel.analyze_run(directory, TABLE, {})


def close(index=12):
    return row("authority_revoked", index, reason="task_finished")


@pytest.mark.parametrize(("proof_request", "manual", "s8", "success"), [
    ("place", False, True, True), ("another", False, False, False), ("place", True, True, False)
])
def test_s8_needs_same_request_and_all_run_assistance(tmp_path, proof_request, manual, s8, success):
    owner, planner = base()
    owner += [row("harness_skill_first_publication", 5, published_ns=4_000_000_000),
              row("skill_accepted", 6, intent=dict(request_id="place", skill="verify_placed")),
              row("owner_placement_verified", 7, request_id=proof_request),
              skill(8, "verify_placed", "place"), skill(9, "withdraw_reset", "reset"), close()]
    if manual:
        planner.append(row("manual_intervention", 1030))
    r = analyze(tmp_path, owner, planner)
    assert (r["stages"]["S8"], r["complete_success"], r["stages"]["S9"]) == (s8, success, True)
    assert r["first_motion_s"] == 1
    assert r["stage_skill_duration_s"]["S8"] == (2 if s8 else None)
    assert r["task_s"] == (5 if s8 else None)


def test_mismatched_and_synthetic_traces_excluded(tmp_path):
    owner, planner = base()
    owner[0]["synthetic"] = True
    planner[0]["session_id"] = "other"
    r = analyze(tmp_path, owner, planner)
    assert r["issues"] == ["synthetic", "task_scope_mismatch"]
    assert r["stages"]["S0"] is None and r["complete_success"] is None


def test_missing_owner_and_truncated_rows_are_unknown(tmp_path):
    owner, planner = base()
    r = analyze(tmp_path, None, planner)
    assert r["status"] == "owner_missing" and r["model_calls"] == 0
    assert r["revocations"] is None and r["stages"]["S0"] is None
    r = analyze(tmp_path, owner, planner)
    assert r["eligible"] and r["stages"]["S0"] is True and r["stages"]["S1"] is None
    assert r["complete_success"] is None
    stats = funnel.summarize([r])
    assert stats["stages"]["S1"]["trials"] == 0 and stats["success_rate"]["n"] == 0


def test_budget_accounting_and_terminal_association(tmp_path):
    owner, planner = base()
    first = skill(4, "transfer_above_hole", "a", outcome="failure", reason="collision", no_motion=True)
    later = skill(5, "transfer_above_hole", "b", outcome="failure", reason="stale", no_motion=True)
    owner.extend([first, later, close(), row("authority_revoked", 13, reason="web_stop_requested"),
                  row("authority_revoked", 14, reason="owner_bridge_closed")])
    planner += [row("task_needs_attention", 1001, reason="old", request_id="a")]
    r = analyze(tmp_path, owner, planner)
    assert r["nonphysical_rejections"] == r["physical_rejections"] == 1
    assert (r["revocations_raw"], r["revocations"], r["active_fault_revocations"]) == (3, 2, 0)
    assert r["last_failure_source"] == "cross_clock_order_unknown" and r["last_failure_reason"] is None
    assert r["nonphysical_termination"] is None
    planner.extend([row("owner_event", 1002, record=later),
                    row("task_needs_attention", 1003, reason="stale", request_id="b")])
    assert analyze(tmp_path, owner, planner)["nonphysical_termination"] is True


def test_model_logical_calls_attempts_and_end_to_end_latency():
    rows = [row("model_request_started", 1, request_id="a"),
            row("model_transport_error", 2, request_id="a", attempt=1, automatic_retry=True, elapsed_s=1.0),
            row("model_transport_timing", 3, request_id="a", attempt=2, origin_write_started=True),
            row("model_response", 4, request_id="a", elapsed_s=3.0),
            row("model_request_started", 5, request_id="b")]
    metrics = funnel.model_metrics(rows)
    assert (metrics["model_calls"], metrics["model_transport_attempts_recorded"]) == (2, 2)
    assert metrics["model_wait_s"] is None and metrics["model_wait_observed_s"] == 3.0
    rows.append(row("model_transport_error", 7, request_id="b", attempt=1, automatic_retry=False,
                    elapsed_s=2.0, origin_write_started=False))
    metrics = funnel.model_metrics(rows)
    assert metrics["model_wait_s"] == 5 and metrics["model_requests_with_origin_evidence"] == 1


def test_missing_synthetic_flag_cannot_certify_placement(tmp_path):
    owner, planner = base()
    result = skill(6, "verify_placed", "v")
    result["result"].pop("synthetic")
    owner += [row("owner_placement_verified", 5, request_id="v"), result, close()]
    r = analyze(tmp_path, owner, planner)
    assert r["stages"]["S8"] is None and r["complete_success"] is None

def test_old_idless_model_transport_does_not_become_zero_calls():
    result = funnel.model_metrics([row("model_transport_error", 3, error_type="ConnectError")])
    assert result["model_calls"] is None and result["model_wait_s"] is None
    assert result["model_transport_errors"] == 1
