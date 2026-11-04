#!/usr/bin/env python3
"""Summarize paired replay; keep historical trajectory and conditional outputs distinct."""
import argparse
import collections
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation


def margins(q):
    q = np.asarray(q)
    return dict(j2_lower_deg=float(q[1]+94), j6_upper_deg=float(60-q[5]),
                wrist_deg=float(110.5-1.025*abs(q[5])-abs(q[6])))


def distribution(values):
    return dict(zip(("min", "median", "max"), np.quantile(values, (0, .5, 1)).tolist()))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--run", default="run-r3")
    a = p.parse_args()
    manifest = json.loads((a.root/a.run/"manifest.json").read_text())
    assert manifest["completed"]
    records = [json.loads(l) for l in (a.root/a.run/"results.jsonl").read_text().splitlines()]
    inputs = [json.loads(l) for l in (a.root/"inputs.jsonl").read_text().splitlines()]
    assert len(records) == len(inputs)*10
    groups = collections.defaultdict(list)
    for r in records:
        groups[r["session_id"]].append(r)
    summary = {"protocol": "same images/state/held/explicit noise/processing/IK; only checkpoint differs",
               "seeds": manifest["seeds"], "records": len(records), "sessions": {}}
    flat = []
    keys = ["j2_lower_deg", "j6_upper_deg", "wrist_deg"]
    for session, rows in groups.items():
        originals = [r["request"] for r in inputs if r["request"]["session_id"] == session]
        first_bad = originals[-1]["request_id"]
        assert not originals[-1]["success"] and all(r["success"] for r in originals[:-1])
        by_pair = collections.defaultdict(dict)
        for r in rows:
            by_pair[(r["request_id"], r["seed"])][r["step"]] = r
        assert all(set(v) == {5000,14000} for v in by_pair.values())
        s = {"first_historical_rejection_request": first_bad, "historical_observations": len(originals),
             "windows": {}, "timeline": []}
        for window, lo, hi in [("all_before", 1, first_bad-1), ("last10_before", first_bad-10, first_bad-1),
                               ("first_rejection_observation", first_bad, first_bad)]:
            pairs = [v for (req, seed), v in by_pair.items() if lo <= req <= hi]
            w = {"pairs": len(pairs), "models": {}}
            for step in (5000,14000):
                subset = [v[step] for v in pairs]
                w["models"][str(step)] = dict(rejected=sum(not r["success"] for r in subset),
                   j2_contact=sum(r["min_candidate_margins"]["j2_lower_deg"] < 1e-5 for r in subset),
                   j6_contact=sum(r["min_candidate_margins"]["j6_upper_deg"] < 1e-5 for r in subset),
                   wrist_contact=sum(r["min_candidate_margins"]["wrist_deg"] < 1e-5 for r in subset))
            w["paired_outcomes"] = dict(collections.Counter(
                f"5K_{'ok' if v[5000]['success'] else 'reject'}__14K_{'ok' if v[14000]['success'] else 'reject'}" for v in pairs))
            # Compare successful full prefixes separately; rejected candidates are not executable actions.
            both = [v for v in pairs if v[5000]["success"] and v[14000]["success"]]
            w["both_success_count"] = len(both)
            w["both_success_margin_14k_minus_5k_deg"] = {k: distribution([
                v[14000]["min_candidate_margins"][k]-v[5000]["min_candidate_margins"][k] for v in both]) for k in keys} if both else {}
            w["both_success_14k_lower_margin_pairs"] = {k: sum(
                v[14000]["min_candidate_margins"][k] < v[5000]["min_candidate_margins"][k]-1e-5 for v in both) for k in keys}
            xyz, rot = [], []
            for v in pairs:
                x, y = np.asarray(v[5000]["target_poses"])[19], np.asarray(v[14000]["target_poses"])[19]
                xyz.append(np.linalg.norm(x[:3,3]-y[:3,3])*1000)
                rot.append(np.rad2deg(Rotation.from_matrix(x[:3,:3].T@y[:3,:3]).magnitude()))
            w["target20_5k_vs_14k_translation_mm"] = distribution(xyz)
            w["target20_5k_vs_14k_rotation_deg"] = distribution(rot)
            s["windows"][window] = w
        t0 = originals[0]["observation_monotonic_ns"]
        for original in originals:
            req = original["request_id"]
            paired = [v for (rid, seed), v in by_pair.items() if rid == req]
            historic_q = [d["candidate_joints"] for d in original["diagnostics"]]
            state = np.asarray(original["prediction_replay"]["state16"])[7:14]
            held = np.asarray(original["prediction_replay"]["last_submitted16"])[7:14]
            line = dict(request=req, t_s=(original["observation_monotonic_ns"]-t0)/1e9,
                        original_success=original["success"], state_margins=margins(state),
                        original_min_candidate_margins={k:min(margins(q)[k] for q in historic_q) for k in keys},
                        j6_state_deg=float(state[5]), j6_held_deg=float(held[5]),
                        state_outside_indices=original.get("state_diagnostics", {}).get("outside_indices", []), models={})
            for step in (5000,14000):
                subset = [v[step] for v in paired]
                line["models"][str(step)] = dict(rejected=sum(not r["success"] for r in subset),
                    margins={k:distribution([r["min_candidate_margins"][k] for r in subset]) for k in keys},
                    j6_end_candidate_deg=distribution([r["candidate_right_deg"][-1][5] for r in subset]))
            s["timeline"].append(line)
            flat.append(dict(session=session, **line))
        summary["sessions"][session] = s
    (a.root/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(len(groups), 3, figsize=(16, 8), squeeze=False)
    titles = ["J2 lower-limit margin", "J6 upper-limit margin", "Wrist coupling margin"]
    for i, (session, s) in enumerate(summary["sessions"].items()):
        lines = s["timeline"]; x = np.array([v["t_s"] for v in lines])
        for j, key in enumerate(keys):
            ax = axes[i,j]
            for step, color in [(5000, "#147D92"), (14000, "#D95432")]:
                values = [v["models"][str(step)]["margins"][key] for v in lines]
                ax.plot(x, [v["median"] for v in values], color=color, label=f"{step//1000}K replay median")
                ax.fill_between(x, [v["min"] for v in values], [v["max"] for v in values], color=color, alpha=.15)
            ax.plot(x, [v["state_margins"][key] for v in lines], color="black", linestyle="--", linewidth=1, label="Historical measured state")
            ax.plot(x, [v["original_min_candidate_margins"][key] for v in lines], color="gray", linestyle=":", linewidth=1, label="Historical 14K prefix")
            ax.axvline(x[-1], color="red", linestyle=":", linewidth=1)
            ax.axhline(0, color="gray", linewidth=.5)
            ax.set_title(f"Session {i+1}: {titles[j]}")
            ax.set_xlabel("Seconds since first request"); ax.set_ylabel("Margin (degrees)")
            ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8)
    fig.suptitle("5K vs 14K | identical historical 14K observations, 5 shared noise tensors\nLower margin = closer to limit; shaded range = seeds; not counterfactual rollouts", fontsize=13)
    fig.tight_layout(rect=(0,0,1,.92)); fig.savefig(a.root/"paired-boundary-margins.png", dpi=160)
    plt.close(fig)
    print(json.dumps({k: v["windows"] for k,v in summary["sessions"].items()}, indent=2))


if __name__ == "__main__":
    main()
