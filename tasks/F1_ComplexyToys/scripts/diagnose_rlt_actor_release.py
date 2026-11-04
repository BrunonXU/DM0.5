#!/usr/bin/env python3
"""CPU-only, zero-update release diagnostics for a frozen F1 RLT BC head."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
import os
from pathlib import Path
import sys

os.environ["CUDA_VISIBLE_DEVICES"] = ""
import numpy as np
import torch


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def distribution(value):
    x = value.detach().flatten().double().numpy()
    if not len(x):
        return {"count": 0}
    return {"count": len(x), "min": float(x.min()), "p10": float(np.quantile(x, .1)),
            "median": float(np.median(x)), "p90": float(np.quantile(x, .9)), "max": float(x.max())}


def plot_svg(path, paths, norm, title, fps):
    """Export exact numerical line plots without changing the locked environment."""
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="650" viewBox="0 0 1100 650">',
             '<rect width="1100" height="650" fill="white"/>',
             '<g font-family="sans-serif" font-size="13" fill="#24292f">',
             f'<text x="35" y="26" font-size="18">{html.escape(title)}</text>']
    titles = ["Gripper: 0 open / 100 closed", "Anchor-relative X (mm)", "Anchor-relative Y (mm)", "Anchor-relative Z (mm)"]
    for panel in range(4):
        px, py = 70 + (panel % 2) * 540, 70 + (panel // 2) * 275
        width, height = 430, 195
        series = []
        for name, values, color in paths:
            raw = values.numpy()
            y = (raw[:, 9] + 1) * 50 if panel == 0 else (raw[:, panel - 1] * norm["std"][panel - 1] + norm["mean"][panel - 1]) * 1000
            series.append((name, y, color))
        lo, hi = (-5., 105.) if panel == 0 else (min(float(y.min()) for _, y, _ in series), max(float(y.max()) for _, y, _ in series))
        if panel:
            pad = max((hi - lo) * .08, .05)
            lo, hi = lo - pad, hi + pad
        def sy(value):
            return py + height * (hi - value) / (hi - lo)
        parts.append(f'<text x="{px}" y="{py-12}" font-size="15">{titles[panel]}</text>')
        for tick in range(5):
            y = lo + (hi - lo) * tick / 4
            yt = sy(y)
            xt = px + width * tick / 4
            parts.extend([f'<path d="M{px},{yt}h{width}" stroke="#e8e8e8" fill="none"/>',
                          f'<text x="{px-8}" y="{yt+4}" text-anchor="end">{y:.2f}</text>',
                          f'<text x="{xt}" y="{py+height+19}" text-anchor="middle">{(15/fps)*tick/4:.3f}</text>'])
        parts.append(f'<path d="M{px},{py}v{height}h{width}" stroke="#24292f" fill="none"/>')
        if panel == 0:
            parts.append(f'<path d="M{px},{sy(50)}h{width}" stroke="#999" stroke-dasharray="4 3" fill="none"/>')
        for name, values, color in series:
            points = [(px + width * k / 15, sy(float(v))) for k, v in enumerate(values)]
            parts.append(f'<polyline points="{" ".join(f"{x:.3f},{y:.3f}" for x,y in points)}" stroke="{color}" stroke-width="2" fill="none"/>')
            parts.extend(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="2.3" fill="{color}"/>' for x, y in points)
        parts.append(f'<text x="{px+width/2}" y="{py+height+40}" text-anchor="middle">Future seconds</text>')
    for i, (name, _, color) in enumerate(paths):
        x = 70 + i * 255
        parts.extend([f'<path d="M{x},619h28" stroke="{color}" stroke-width="3"/>',
                      f'<text x="{x+36}" y="624">{html.escape(name)}</text>'])
    parts.append('</g></svg>')
    Path(path).write_text("\n".join(parts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    task = args.release / "tasks/F1_ComplexyToys"
    sys.path.insert(0, str(task / "src"))
    sys.path.insert(0, str(task / "scripts"))
    from f1_toys.rlt_stage2 import make_policy, actor_parameters, bc_targets
    from f1_toys.rlt_human_replay import HUMAN_SOURCES
    from f1_toys.eef import encode_actions, normalize_actions
    from f1_toys.native.kinematics import ArmModel
    from rlt_dagger_train import sample_indices
    import wandb

    torch.set_num_threads(4)
    torch.manual_seed(20261026)
    cache = args.run_root / "cache"
    manifest = json.loads((cache / "manifest.json").read_text())
    cfg = json.loads((args.run_root / "recipe.json").read_text())
    checkpoint = args.run_root / "bc/step-000400.pt"
    source_sha, checkpoint_sha = digest(__file__), digest(checkpoint)
    for name, expected in manifest["files_sha256"].items():
        if digest(cache / name) != expected:
            raise ValueError("Frozen replay hash mismatch: " + name)
    data = torch.load(cache / "replay.pt", map_location="cpu", weights_only=True)
    records = [json.loads(line) for line in (cache / "transitions.jsonl").read_text().splitlines()]
    run = wandb.init(project=cfg["wandb"]["project"], entity=cfg["wandb"]["entity"],
        group="10_RLT_Training_Diagnostics", job_type="actor-release-evaluation",
        id=args.run_id, name="B1250_RLT_BC400_Release_Diagnostic", resume="never", dir=str(args.output),
        mode=os.environ.get("WANDB_MODE", "online"),
        tags=["F1", "RLT", "BC400", "CPU", "zero_parameter_update", "release_diagnostic"],
        config={"diagnostic_sha256": source_sha, "checkpoint_sha256": checkpoint_sha,
                "replay_manifest_sha256": digest(cache / "manifest.json"), "parameter_updates": 0})
    try:
        saved = torch.load(checkpoint, map_location="cpu", weights_only=False)
        model = make_policy(args.release / "RLinf", cfg["model"]).eval()
        model.load_state_dict(saved["model"], strict=True)
        before = {key: value.detach().clone() for key, value in model.state_dict().items()}
        obs = {key: data[key][data["current"]] for key in ("z_rl", "proprio", "ref_chunk")}
        zero_obs = {**obs, "ref_chunk": torch.zeros_like(obs["ref_chunk"])}
        with torch.no_grad():
            actor = model.sac_forward(obs, deterministic=True)[0].reshape(-1, 16, 10)
            no_ref = model.sac_forward(zero_obs, deterministic=True)[0].reshape_as(actor)
            reference = model._get_ref_chunk(obs).reshape_as(actor)
        human = data["actions"]
        flags = data["intervene_flags"]
        target, checked_flags = bc_targets(model, obs, human, flags)
        assert torch.equal(checked_flags, flags)
        assert torch.equal(target[flags], human[flags])
        assert torch.equal(target[~flags], reference[~flags])
        assert torch.equal(flags, torch.tensor([r["intervene_flags"] for r in records]))
        labels = [r.get("phase", "autonomous") for r in records]
        human_rows = [i for i, r in enumerate(records) if r["data_source"] == "human_correction"]
        assert flags[human_rows].all()
        assert not flags[[i for i, r in enumerate(records) if r["data_source"] != "human_correction"]].any()
        norm = manifest["action_norm_stats"]
        arm = ArmModel(task / "src/f1_toys/native/ccs_m6_40.MvKDCfg")
        action_error, raw_controls_checked, raw_sources = 0., 0, {}
        for source in sorted({records[i]["source_run"] for i in human_rows}):
            path = Path(cfg["human_raw_root"]) / source / "journal/control.jsonl"
            controls = {}
            for line in path.read_text().splitlines():
                row = json.loads(line)
                controls[row["stream_sequence"]] = row
            for i in [i for i in human_rows if records[i]["source_run"] == source]:
                record = records[i]
                raw = [controls[s] for s in record["action_control_sequences"]]
                assert [r["submitted"] for r in raw] == record["submitted16"]
                assert [r["source"] for r in raw] == record["action_sources"]
                assert all(r["source"] in HUMAN_SOURCES for r in raw)
                rebuilt = normalize_actions(encode_actions(arm, record["state16"], np.asarray(record["submitted16"])), norm)[..., :10]
                action_error = max(action_error, float(np.max(np.abs(rebuilt - human[i].numpy()))))
                raw_controls_checked += len(raw)
            raw_sources[source] = {"control_sha256": digest(path), "rows": len(controls)}
        if action_error != 0:
            raise ValueError("Cached human labels differ from original submitted FK labels")
        print("MASK_AND_RAW_LABELS_PASS", flush=True)

        rng = np.random.default_rng(cfg["seed"])
        counts = Counter()
        sampling = {}
        for step in range(1, 551):
            counts.update(sample_indices(records, rng, cfg))
            if step in (400, 550):
                by_phase = Counter()
                for i, count in counts.items():
                    by_phase[labels[i]] += count
                sampling[str(step)] = {"draws": sum(counts.values()), "phase_draws": dict(by_phase),
                    "release_transition_fraction": by_phase["release_transition"] / sum(counts.values()),
                    "terminal_draws": [{"index": i, "source_run": r["source_run"], "outcome": r["segment_outcome"], "draws": counts[i]}
                                       for i, r in enumerate(records) if r["split"] == "train" and r["terminated"]]}
                if step == 400:
                    weights = torch.tensor([counts[i] / sum(counts.values()) for i in range(len(records))])

        metric_rows = []
        for split in ("train", "validation"):
            for label in ["all_human"] + sorted(set(labels)):
                chosen = torch.tensor([r["split"] == split and r["data_source"] == "human_correction" and
                                       (label == "all_human" or labels[i] == label) for i, r in enumerate(records)])
                if not chosen.any():
                    continue
                mask = flags[chosen]
                truth = human[chosen]
                for name, predictions in (("B1250_reference", reference), ("BC400", actor), ("BC400_no_reference", no_ref)):
                    pred = predictions[chosen]
                    err = pred - truth
                    should_open = mask & (truth[..., 9] < 0)
                    should_hold = mask & ~should_open
                    missed = should_open & (pred[..., 9] >= 0)
                    false = should_hold & (pred[..., 9] < 0)
                    components = [float(err[..., s].square().sum() / err.numel()) for s in (slice(0, 3), slice(3, 9), slice(9, 10))]
                    metric_rows.append({"split": split, "phase": label, "model": name, "chunks": int(chosen.sum()),
                        "expert_mse": float(err.square().mean()), "xyz_mse_contribution": components[0],
                        "rotation_mse_contribution": components[1], "gripper_mse_contribution": components[2],
                        "xyz_mm_mae": float((err[..., :3] * torch.tensor(norm["std"])).abs().mean() * 1000),
                        "gripper_physical_mae": float(err[..., 9].abs().mean() * 50),
                        "should_open": int(should_open.sum()), "missed_open": int(missed.sum()),
                        "should_hold": int(should_hold.sum()), "false_open": int(false.sum())})

        # Exact expected 50% reference dropout objective at this fixed checkpoint.
        p_on = model.sac_forward(obs, deterministic=True)[0].reshape_as(human)
        p_off = model.sac_forward(zero_obs, deterministic=True)[0].reshape_as(human)
        per_element = .5 * ((p_on - target).square() + (p_off - target).square())
        weighted = per_element * weights[:, None, None] / (16 * 10)
        objective = cfg["bc_weight"] * weighted.sum()
        parameters = actor_parameters(model)
        def gradient(loss):
            values = torch.autograd.grad(loss, parameters, retain_graph=True, allow_unused=False)
            return torch.cat([value.flatten() for value in values])
        total_gradient = gradient(objective)
        grad_report = {"total_bc_loss": float(objective.detach()), "total_gradient_norm": float(total_gradient.norm()),
                       "reference_dropout_expectation": .5, "sampling_weights": "exact replay of first 400 training updates", "components": {}, "phases": {}}
        for name, dims in (("xyz", slice(0, 3)), ("rotation6d", slice(3, 9)), ("gripper", slice(9, 10))):
            value = cfg["bc_weight"] * weighted[..., dims].sum()
            g = gradient(value)
            grad_report["components"][name] = {"loss": float(value.detach()), "gradient_norm": float(g.norm()),
                "cosine_to_total": float(torch.nn.functional.cosine_similarity(g, total_gradient, dim=0)),
                "gradient_projection_fraction": float(torch.dot(g, total_gradient) / total_gradient.square().sum())}
        for label in sorted(set(labels)):
            choose = torch.tensor([x == label for x in labels])
            if weights[choose].sum() == 0:
                continue
            value = cfg["bc_weight"] * weighted[choose].sum()
            g = gradient(value)
            grad_report["phases"][label] = {"loss": float(value.detach()), "gradient_norm": float(g.norm()),
                "cosine_to_total": float(torch.nn.functional.cosine_similarity(g, total_gradient, dim=0)),
                "gradient_projection_fraction": float(torch.dot(g, total_gradient) / total_gradient.square().sum())}
        del p_on, p_off, per_element, weighted
        print("GRADIENTS_NO_OPTIMIZER_PASS", flush=True)

        open_mask = flags & (human[..., 9] < 0)
        closed_mask = flags & ~open_mask
        transition = torch.tensor([label == "release_transition" for label in labels])[:, None].expand(-1, 16)
        saturation = {}
        for name, mask in (("transition_should_open_but_actor_closed", transition & open_mask & (actor[..., 9] >= 0)),
                           ("ordinary_holding", closed_mask & ~transition), ("already_open", open_mask & ~transition)):
            saturation[name] = {"tanh_output": distribution(actor[..., 9][mask]),
                                "tanh_derivative": distribution((1 - actor[..., 9].square())[mask]),
                                "no_reference_output": distribution(no_ref[..., 9][mask])}

        plots, events, waiting = [], [], []
        for source in sorted({records[i]["source_run"] for i in human_rows}):
            ids = sorted([i for i in human_rows if records[i]["source_run"] == source], key=lambda i: records[i]["decision_ns"])
            mixed = [i for i in ids if (human[i, :, 9] < 0).any() and (human[i, :, 9] >= 0).any()]
            selected = mixed or [next(i for i in ids if (human[i, :, 9] < 0).any())]
            for i in selected:
                paths = [("human", human[i], "black"), ("B1250", reference[i], "#c78b00"),
                         ("actor400", actor[i], "#0969da"), ("actor no ref", no_ref[i], "#a43bbc")]
                title = f"{records[i]['split']} | {source[-8:]} | transition {i} | recorded human boundary"
                name = f"release-{source[-8:]}-{i}.svg"
                plot_svg(args.output / name, paths, norm, title, cfg["fps"]); plots.append(name)
                event = {"index": i, "source_run": source, "split": records[i]["split"], "mixed_open_closed_chunk": i in mixed,
                         "phase": labels[i], "plot": name,
                         "gripper": {name: ((value[:, 9].numpy() + 1) * 50).tolist() for name, value, _ in paths},
                         "eef_xyz_mm": {name: ((value[:, :3].numpy() * np.asarray(norm["std"]) + np.asarray(norm["mean"])) * 1000).tolist() for name, value, _ in paths}}
                events.append(event)
                # Compare earlier genuine held-object labels from the same episode.
                prior = [j for j in ids if 0 < records[i]["decision_ns"] - records[j]["decision_ns"] <= 2_500_000_000
                         and (human[j, :, 9] >= 0).all() and records[j]["state16"][15] >= 50]
                if prior:
                    prior.sort(key=lambda j: float(np.max(np.abs(np.asarray(records[j]["state16"])[7:14] - np.asarray(records[i]["state16"])[7:14]))))
                    j = prior[0]
                    z0, z1 = obs["z_rl"][j], obs["z_rl"][i]
                    waiting.append({"release_index": i, "prior_index": j, "source_run": source,
                        "gap_seconds": (records[i]["decision_ns"] - records[j]["decision_ns"]) / 1e9,
                        "right_joint_max_delta_degrees": float(np.max(np.abs(np.asarray(records[j]["state16"])[7:14] - np.asarray(records[i]["state16"])[7:14]))),
                        "proprio_l2": float((obs["proprio"][j] - obs["proprio"][i]).norm()),
                        "token_cosine": float(torch.nn.functional.cosine_similarity(z0, z1, dim=0)),
                        "token_rms_delta": float((z0 - z1).square().mean().sqrt()),
                        "reference_gripper_rms_delta": float((reference[j, :, 9] - reference[i, :, 9]).square().mean().sqrt()),
                        "actor_gripper_rms_delta": float((actor[j, :, 9] - actor[i, :, 9]).square().mean().sqrt()),
                        "raw_expert_future_differs": bool((human[j, :, 9] != human[i, :, 9]).any())})
        unchanged = all(torch.equal(before[key], value) for key, value in model.state_dict().items())
        assert unchanged and digest(checkpoint) == checkpoint_sha
        result = {"schema": "f1_rlt_actor_release_diagnostic_v1", "checkpoint_step": 400,
            "checkpoint_sha256": checkpoint_sha, "diagnostic_sha256": source_sha,
            "parameter_updates": 0, "optimizer_constructed": False, "parameters_bit_identical": unchanged,
            "device": "cpu", "vla_loaded": False, "encoder_loaded": False, "robot_commands": 0,
            "mask_audit": {"human_target_equals_actual": True, "autonomous_target_equals_reference": True,
                           "raw_control_rows_checked": raw_controls_checked, "raw_fk_label_max_error": action_error,
                           "allowed_human_sources": sorted(HUMAN_SOURCES),
                           "raw_sources": raw_sources},
            "sampling": sampling, "phase_metrics": metric_rows, "gradient_decomposition": grad_report,
            "saturation": saturation, "events": events, "prior_waiting_pairs": waiting,
            "wandb_url": run.url,
            "limitations": ["Reference is newly cached unguided B1250, not the historical RTC proposal.",
                            "C16 open/closed counts concern model commands, not measured physical release.",
                            "Reference-zero is an intervention on model input, not a tested deployment policy.",
                            "Similar token and joints do not prove identical visual information or causal ambiguity.",
                            "Gradient decomposition is at BC400 with exact expected reference dropout and replay sampling weights; it is not the history of all updates."]}
        dump(args.output / "report.json", result)
        torch.save({"human": human, "reference": reference, "actor400": actor, "actor400_no_reference": no_ref}, args.output / "predictions.pt")
        dump(args.output / "wandb.json", {"id": run.id, "url": run.url})
        run.log({"audit/parameter_updates": 0, "audit/parameters_bit_identical": int(unchanged),
                 "audit/raw_fk_max_error": action_error, "sampling/release_transition_fraction_400": sampling["400"]["release_transition_fraction"],
                 **{"gradient/" + key + "_norm": value["gradient_norm"] for key, value in grad_report["components"].items()}})
        for row in metric_rows:
            if row["phase"] in ("all_human", "release_transition"):
                prefix = row["split"] + "/" + row["phase"] + "/" + row["model"] + "/"
                run.log({prefix + key: row[key] for key in ("expert_mse", "xyz_mm_mae", "missed_open", "false_open")})
        run.summary.update({"parameter_updates": 0, "parameters_bit_identical": True, "release_events": len(events),
                            "raw_control_rows_checked": raw_controls_checked, "robot_commands": 0})
        print(json.dumps({"status": "PASS", "output": str(args.output), "wandb": run.url, "events": len(events)}), flush=True)
    except BaseException:
        run.finish(exit_code=1)
        raise
    run.finish()


if __name__ == "__main__":
    main()
