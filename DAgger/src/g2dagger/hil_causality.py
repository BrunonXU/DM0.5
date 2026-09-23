"""Offline cross-stream checks; never infer hardware execution from selection."""
import numpy as np


def audit_causality(records, manifest):
    errors = []
    observations, chunks = {}, {}
    def check(ok, message):
        if not ok:
            errors.append(message)
    for stream, rows in records.items():
        for row in rows:
            check(row.get("episode_id") == manifest.get("episode_id"), f"{stream}: episode ID mismatch")
    for row in records["observations"]:
        key = row.get("request_id")  # legacy name: observation ID, not inference request ID
        check(isinstance(key, int) and key >= 0 and key not in observations, "duplicate/invalid observation ID")
        check(row.get("observation_id", key) == key, "observation ID alias mismatch")
        observations[key] = row
    for row in records["policy_chunks"]:
        try:
            key = (row["policy_epoch"], row["request_id"])
            check(key not in chunks, "duplicate policy request")
            chunks[key] = row
            obs = observations[row["observation_request_id"]]
            t = obs["monotonic_ns"]
            check(row["observation_monotonic_ns"] == t, "policy observation timestamp mismatch")
            check(t <= row["request_monotonic_ns"] <= row["response_monotonic_ns"], "noncausal policy request/response")
            if "acquisition" in obs:
                check(obs["acquisition"]["available_monotonic_ns"] <= row["request_monotonic_ns"],
                      "policy request precedes observation availability")
            check(obs["policy_epoch"] == key[0], "policy observation epoch mismatch")
        except (KeyError, TypeError, ValueError):
            errors.append("missing/invalid policy observation reference")
    if any(r.get('event') == 'runtime_waiting_for_start' for r in records['events']):
        starts = [r['monotonic_ns'] for r in records['events']
                  if r.get('event') == 'operator_start_authorized']
        check(len(starts) <= 1, 'multiple initial START authorizations')
        for chunk in records['policy_chunks']:
            check(bool(starts) and chunk['observation_monotonic_ns'] > starts[0],
                  'policy observation precedes initial START')
        for row in records['control']:
            check(bool(starts) and row['monotonic_ns'] >= starts[0],
                  'dispatch precedes initial START')
    handoffs = {}
    for row in records["events"]:
        try:
            if row["event"] == "hil_to_handoff_hold":
                handoffs[row["policy_epoch"]] = row["monotonic_ns"]
            if row["event"] == "fresh_policy_to_policy":
                chunk = chunks[(row["policy_epoch"], row["policy_request_id"])]
                check(handoffs[row["policy_epoch"]] < chunk["observation_monotonic_ns"], "fresh observation predates handoff")
                check(chunk["response_monotonic_ns"] <= row["monotonic_ns"], "acceptance precedes response")
        except (KeyError, TypeError):
            errors.append("invalid fresh-policy event reference")
    sequences = [r.get("control_sequence") for r in records["control"]]
    check(sequences == list(range(len(sequences))), "control_sequence is not contiguous")
    valid_sources = {"policy": "policy", "policy_wait": "policy", "home": "hil_latched", "hold": None, "human_hold": "hil_latched",
                     "human_arm": "hil_latched", "human_hand": "hil_latched", "human_arm_and_hand": "hil_latched"}
    transitions = {"policy_to_hil": "hil_latched", "hold_to_hil": "hil_latched",
                   "hil_to_handoff_hold": "handoff_hold", "fresh_policy_to_policy": "policy",
                   "safe_hold_entered": "safe_hold"}
    edges = [event for event in records["events"] if event.get("event") in transitions]
    obs_edge_index, obs_state, obs_epoch = 0, "policy", 0
    for observation in records["observations"]:
        while obs_edge_index < len(edges) and edges[obs_edge_index]["monotonic_ns"] <= observation["monotonic_ns"]:
            edge = edges[obs_edge_index]
            obs_state, obs_epoch = transitions[edge["event"]], edge["policy_epoch"]
            obs_edge_index += 1
        check(observation["hil_state"] == obs_state and observation["policy_epoch"] == obs_epoch,
              "observation disagrees with ownership event history")
    edge_index, state, epoch = 0, "policy", 0
    sampling_starts = {}
    for row in records["control"]:
        try:
            while edge_index < len(edges) and edges[edge_index]["monotonic_ns"] <= row["monotonic_ns"]:
                edge = edges[edge_index]
                state, epoch = transitions[edge["event"]], edge["policy_epoch"]
                edge_index += 1
            check(row["hil_state"] == state and row["policy_epoch"] == epoch,
                  "control disagrees with ownership event history")
            source = row["control_source"]
            check(source in valid_sources, "unknown control source")
            check(row["hil_state"] == valid_sources[source] if source != "hold" else row["hil_state"] in ("handoff_hold", "safe_hold"), "control source/state mismatch")
            expected_masks = (source.startswith("human_") or source == 'home', source in ("human_arm", "human_arm_and_hand"),
                              source in ("human_hand", "human_arm_and_hand"))
            check(tuple(row[k] for k in ("intervention_mask", "human_arm_mask", "human_hand_mask")) == expected_masks,
                  "control masks disagree with source")
            for call in row.get("sdk_calls", []):
                check(row["monotonic_ns"] <= call["started_monotonic_ns"] <= call["finished_monotonic_ns"],
                      "noncausal SDK call")
                indices, targets = call["joint_indices"], call["submitted_positions"]
                check(len(indices) == len(targets) and len(set(indices)) == len(indices)
                      and all(type(i) is int and 0 <= i < 34 for i in indices), "invalid SDK joint layout")
                check(bool(call["interface"]) and "sdk_return" in call, "missing SDK result/interface")
                submitted = row["submitted_command_34d"]
                check(submitted is not None and np.array_equal(np.asarray(submitted)[indices], targets),
                      "SDK submitted target disagrees with submitted 34D")
            if source == "policy":
                chunk = chunks[(row["policy_epoch"], row["policy_request_id"])]
                check(chunk["response_monotonic_ns"] <= row["monotonic_ns"], "control precedes policy response")
                idx = row["policy_action_index"]
                check(isinstance(idx, int) and 0 <= idx < 50, "invalid action index")
                sampling = row.get('policy_sampling')
                if sampling is None:
                    check(np.array_equal(chunk["actions_50x34"][idx], row["policy_proposal_34d"]), "policy proposal differs from referenced waypoint")
                else:
                    check(sampling['kind'] == 'linear_anchor_30hz' and sampling['source_hz'] == 30., 'invalid policy sampling contract')
                    start = sampling['chunk_started_monotonic_ns']
                    check(chunk['response_monotonic_ns'] <= start <= row['monotonic_ns'], 'invalid policy sampling time')
                    u = max(0., (row['monotonic_ns']-start)/1e9*30.)
                    lo = -1 if u < 1 else int(min(u-1, 49.))
                    hi = 0 if lo == -1 else min(lo+1, 49)
                    alpha = u if lo == -1 else min(u-1, 49.)-lo
                    check((sampling['lower_index'], sampling['upper_index']) == (lo, hi)
                          and abs(sampling['alpha']-alpha) <= 1e-12, 'policy sampling indices/alpha disagree with time')
                    check(idx == max(0, lo), 'policy action index disagrees with sampling')
                    check(np.array_equal(sampling['raw_waypoint_34d'], chunk['actions_50x34'][idx]), 'raw policy waypoint mismatch')
                    key = (row['policy_epoch'], row['policy_request_id'])
                    if key not in sampling_starts:
                        check(start == row['monotonic_ns'] and lo == -1, 'missing initial policy anchor tick')
                        sampling_starts[key] = (start, row['hold_34d'])
                    saved_start, anchor = sampling_starts[key]
                    check(start == saved_start, 'policy sampling start changed')
                    if lo == -1:
                        check(np.array_equal(sampling['anchor_34d'], anchor), 'policy anchor differs from submitted hold')
                    left = np.asarray(anchor if lo == -1 else chunk['actions_50x34'][lo])
                    expected = (1-alpha)*left + alpha*np.asarray(chunk['actions_50x34'][hi])
                    check(np.allclose(expected, row['policy_proposal_34d'], rtol=0, atol=1e-12), 'interpolated policy proposal mismatch')
                if row["policy_epoch"] in handoffs:
                    check(chunk["observation_monotonic_ns"] > handoffs[row["policy_epoch"]], "selected stale handoff observation")
        except (KeyError, TypeError, IndexError, ValueError):
            errors.append("invalid control policy reference")
    return errors
