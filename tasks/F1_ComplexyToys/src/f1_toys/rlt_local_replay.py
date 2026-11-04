"""Verified local-placement journal prefixes and actual-action transitions.

No robot commands, reward inference, or modification of raw recordings. The
physical episode may still be open when its durable local prefix is consumed.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from pathlib import Path

OBJECTIVE = 'approach_place_release_v1'
RIGHT = (7, 8, 9, 10, 11, 12, 13, 15)
HUMAN = {'human_arm', 'human_hand', 'human_hold', 'human_arm_and_hand'}
PAUSED = {'input_pause_hold', 'input_recovery_anchor', 'ik_recovery_hold',
          'rtc_timing_hold', 'hold', 'human_compute_wait'}


def read_prefix(run, receipt):
    run = Path(run)
    if (receipt.get('schema') != 'f1_local_journal_prefix_v1' or receipt.get('drop_count') != 0
            or receipt['binding'].get('execution_mode') != 'powered'
            or receipt['binding'].get('recording_boundary') not in ('f1_rlt_local_v2', 'f1_rlt_local_v3', 'f1_rlt_local_v4')):
        raise ValueError('Untrusted local prefix contract')
    streams = {}
    for name in ('observations', 'control', 'events', 'policy_chunks'):
        info = receipt['streams'][name]
        directory = run/'journal' if (run/'journal').is_dir() else run/'journal.partial'
        with (directory/(name+'.jsonl')).open('rb') as handle:
            data = handle.read(info['bytes'])
        if len(data) != info['bytes'] or hashlib.sha256(data).hexdigest() != info['sha256']:
            raise ValueError('Local prefix incomplete or corrupt: ' + name)
        streams[name] = [json.loads(line) for line in data.splitlines()]
        if any(row['episode_id'] != receipt['episode_id'] or row['stream_sequence'] != i
               for i, row in enumerate(streams[name])):
            raise ValueError('Episode or stream sequence mismatch')
    result = receipt['local_result']
    if result not in streams['events'] or result.get('objective') != OBJECTIVE:
        raise ValueError('Local terminal lacks journal evidence')
    return streams


def _valid_observation(row, decision):
    mask = row.get('state_valid_mask', [True]*16)
    return (len(mask) == 16 and all(mask[i] is True for i in RIGHT)
            and len(row.get('state16', ())) == 16
            and all(math.isfinite(v) for v in row['state16'])
            and 0 <= decision-row['available_monotonic_ns'] <= 50_000_000
            and all(0 <= decision-row['state_timestamps_ns'][i] <= 250_000_000 for i in RIGHT)
            and not row.get('camera_discontinuity', False)
            and all(0 <= decision-f['host_received_monotonic_ns'] <= 200_000_000 for f in row['frames']))


def build_transitions(streams, result, *, gamma=0.999, fps=30, chunk_len=16):
    """Split at ownership changes; preserve partial prefixes and elapsed time.

    Unknown/interrupted outcomes are quarantined, never silently made failures.
    Padding is storage only; both BC and Q consumers must use action_time_mask.
    Sensor/control-time gaps split the dataset; bootstrap only from the actual
    valid endpoint on the same side of a gap, never from a later recovered state.
    """
    prefix_only = result.get('status') == 'nonterminal_prefix' and result.get('nonterminal_confirmed') is True
    if result.get('objective') != OBJECTIVE or (result['status'] not in ('success', 'failure') and not prefix_only):
        raise ValueError('Local outcome needs review')
    if result.get('terminated') is not (not prefix_only) or result.get('physical_reset') is not False:
        raise ValueError('Expected local task terminal without physical reset')
    expected_reward = float(result['status'] == 'success')
    if result['reward'] != expected_reward:
        raise ValueError('Outcome and sparse reward disagree')
    terminal = result.get('terminal_observation')
    if not terminal:
        raise ValueError('Missing pre-handoff terminal observation')
    observations = streams['observations']
    controls = streams['control']
    ot = [r['available_monotonic_ns'] for r in observations]
    ct = [r['monotonic_ns'] for r in controls]
    if not ot or not ct or any(b <= a for a, b in zip(ot, ot[1:])) or any(b <= a for a, b in zip(ct, ct[1:])):
        raise ValueError('Source timestamps must increase')
    end = terminal['available_monotonic_ns']
    ti = bisect.bisect_left(ot, end)
    if ti == len(ot) or observations[ti]['observation_id'] != terminal['observation_id'] or ot[ti] != end or end > result['ended_ns']:
        raise ValueError('Terminal observation identity mismatch')
    start = result['started_ns']
    if not start < end or not _valid_observation(observations[ti], end):
        raise ValueError('Invalid terminal clock or sensors')
    local_obs = [r for r in observations if start <= r['available_monotonic_ns'] <= end]
    local_controls = [r for r in controls if start <= r['monotonic_ns'] <= end and r.get('submitted') is not None]
    if len(local_obs) > 1 and len(local_controls) > 1:
        stationary = all(all(r['state16'][j] == local_obs[0]['state16'][j] for r in local_obs) for j in range(7, 14))
        commanded_motion = any(any(r['submitted'][j] != local_controls[0]['submitted'][j] for r in local_controls) for j in range(7, 14))
        if stationary and commanded_motion:
            raise ValueError('Right-arm feedback stationary despite changing joint commands; review execution')
    # Publication provenance includes pre-local persistent gripper commands.
    from .rlt_human_replay import publication_provenance
    provenance = publication_provenance(controls)

    def valid_control(row):
        source = row.get('source')
        sample = row.get('policy_sampling') or {}
        task = sample.get('local_task') or {}
        if (row.get('execution_mode') != 'powered' or row.get('publication_complete') is not True
                or row.get('stage') != 'completed' or row.get('submitted') is None
                or task.get('segment_id') != result['segment_id'] or task.get('status') != 'active'
                or sample.get('task_phase') != 'placement'
                or source not in HUMAN | PAUSED | {'policy', 'rlt_boundary_hold', 'hold', 'takeover_hold'}):
            return False
        mask = row.get('action_valid_mask', [True]*16)
        stamps = row.get('action_source_timestamps_ns', ())
        return (len(stamps) == 16 and len(mask) == 16
                and all(mask[i] is True and isinstance(stamps[i], int)
                        and 0 < stamps[i] <= row['monotonic_ns']
                        and provenance.get((i, stamps[i])) == row['submitted'][i] for i in RIGHT))

    # Every included tick must have actual publication evidence. A command
    # sequence gap/reset still requires review: missing publications cannot be
    # reconstructed. A time gap with consecutive publications instead cuts the
    # dataset below; no action or transition is synthesized across that gap.
    lo, hi = bisect.bisect_left(ct, start), bisect.bisect_right(ct, end)
    selected = controls[lo:hi]
    invalid = next((r for r in selected if not valid_control(r)), None)
    if not selected or invalid is not None:
        detail = 'empty control interval' if invalid is None else (
            f"source={invalid.get('source')!r}, command_sequence={invalid.get('command_sequence')}")
        raise ValueError('Unpublished, non-local or invalid control inside local task: ' + detail)
    if any(b['command_sequence'] != a['command_sequence']+1 for a, b in zip(selected, selected[1:])):
        raise ValueError('Control gap; local task requires review')
    first = max(start, selected[0]['monotonic_ns'])
    control_runs = []
    control_begin = first
    for a, b in zip(selected, selected[1:]):
        if b['monotonic_ns']-a['monotonic_ns'] > 50_000_000:
            control_runs.append((control_begin, a['monotonic_ns']))
            control_begin = b['monotonic_ns']
    control_runs.append((control_begin, end))
    boundaries = [first]
    key = lambda r: (r['policy_epoch'], r['intervention_id'], r['source'] in HUMAN, r['source'] in PAUSED)
    for a, b in zip(selected, selected[1:]):
        if key(a) != key(b):
            boundaries.append(b['monotonic_ns'])
    boundaries.append(end)
    # Keep only continuous runs of fresh, causal observations within each
    # verified control interval. Before a control-time gap the last endpoint
    # must be an observation already available by the preceding publication;
    # after it, capture must start at/after the resumed actual publication.
    # Quality cuts are not fabricated failures. In particular truncated rows
    # retain their real duration/discount and bootstrap from the pre-gap endpoint.
    runs = []
    for control_run_id, (control_begin, control_end) in enumerate(control_runs):
        current = []
        capture_floor = start if control_run_id == 0 else control_begin
        for i in range(bisect.bisect_left(ot, control_begin), bisect.bisect_right(ot, control_end)):
            observation, stamp = observations[i], ot[i]
            valid = (_valid_observation(observation, stamp)
                     and observation['capture_started_monotonic_ns'] >= capture_floor)
            if (not valid or (current and stamp-ot[current[-1]] > 50_000_000)):
                if len(current) > 1:
                    runs.append((current, 'observation_quality_gap'))
                current = []
            if valid:
                current.append(i)
        if len(current) > 1:
            runs.append((current, 'control_quality_gap' if control_end < end else 'observation_quality_gap'))
    records = []
    period = 1e9/fps
    for run_id, (run, quality_reason) in enumerate(runs):
        run_start, run_end = ot[run[0]], ot[run[-1]]
        for begin, stop in zip(boundaries, boundaries[1:]):
            owner_row = controls[bisect.bisect_right(ct, begin)-1]
            if owner_row['source'] in PAUSED:
                continue  # Verified raw HOLD remains recorded, not an actor action.
            after_index = bisect.bisect_left(ct, stop)
            pause_cut = after_index < len(controls) and controls[after_index]['source'] in PAUSED
            t, stop = max(begin, run_start), min(stop, run_end)
            while t < stop:
                cap = min(stop, t+round(chunk_len*period))
                # Full chunks end at an actually available observation, rather
                # than an arbitrary grid point that can make a fresh frame old.
                # Ownership and local-terminal boundaries still cut immediately.
                nxt = cap if cap == stop else ot[bisect.bisect_right(ot, cap)-1]
                if nxt <= t:
                    raise ValueError('No forward causal observation in valid run')
                oi, ni = bisect.bisect_right(ot, t)-1, bisect.bisect_right(ot, nxt)-1
                if not _valid_observation(observations[oi], t) or not _valid_observation(observations[ni], nxt):
                    raise ValueError('Missing fresh causal observation inside valid run')
                count = min(chunk_len, math.ceil((nxt-t)/period))
                grid = [t+round(i*period) for i in range(count)]
                ticks = [controls[bisect.bisect_right(ct, stamp)-1] for stamp in grid]
                if any(not valid_control(r) or not 0 <= stamp-r['monotonic_ns'] <= 50_000_000 for r, stamp in zip(ticks, grid)):
                    raise ValueError('Invalid sampled action')
                final = nxt == end
                duration_steps = (nxt-t)/period
                records.append(dict(schema='f1_local_transition_v2', objective=OBJECTIVE,
                    episode_id=result['episode_id'], segment_id=result['segment_id'],
                    observation_id=observations[oi]['observation_id'], next_observation_id=observations[ni]['observation_id'],
                    state16=observations[oi]['state16'], decision_ns=t, next_decision_ns=nxt,
                    elapsed_steps=duration_steps, discount=gamma**duration_steps,
                    submitted16=[r['submitted'] for r in ticks],
                    action_time_mask=[True]*count+[False]*(chunk_len-count),
                    intervene_flags=[r['source'] in HUMAN for r in ticks]+[False]*(chunk_len-count),
                    command_sequences=[r['command_sequence'] for r in ticks],
                    reward=expected_reward if final else 0., terminated=final and not prefix_only,
                    reward_known=True, terminal_known=True, next_state_verified=True,
                    truncated=(final and prefix_only) or ((nxt == run_end or (nxt == stop and pause_cut)) and not final),
                    observation_run=run_id,
                    boundary_reason=('local_terminal' if final else
                        quality_reason if nxt == run_end else
                        'protective_pause' if nxt == stop and pause_cut else
                        'control_owner' if nxt == stop else 'chunk'),
                    actor_version=result['actor_version'], assisted=result['assisted']))
                # This is the owner-approved reference that produced these
                # actual publications, never an unexecuted policy proposal or
                # rejected human candidate. Legacy sources may not have it.
                if all(r.get('selected') is not None for r in ticks):
                    records[-1]['selected16'] = [r['selected'] for r in ticks]
                t = nxt
    if not records or sum(r['terminated'] for r in records) != int(not prefix_only):
        raise ValueError('No valid local terminal transition')
    return records


def terminal_suffixes(streams, result, *, gamma=.999):
    """More actual observation anchors ending at the SAME reviewed terminal.

    These overlapping windows are critic calibration only, not new episodes or
    extra interaction time. Stay inside the final valid ownership/sensor run.
    """
    final = build_transitions(streams, result, gamma=gamma)[-1]
    records = {final['decision_ns']: final}
    for observation in streams['observations']:
        stamp = observation['available_monotonic_ns']
        if not final['decision_ns'] < stamp < final['next_decision_ns']:
            continue
        # Capture starts before a completed observation becomes available.
        # Using availability as a new capture boundary would discard that
        # very observation, including the penultimate terminal anchor.
        shifted = dict(result, started_ns=max(result['started_ns'], observation['capture_started_monotonic_ns']))
        row = build_transitions(streams, shifted, gamma=gamma)[-1]
        if not row['terminated'] or row['next_decision_ns'] != final['next_decision_ns']:
            raise ValueError('Terminal suffix changed the task endpoint')
        if row['decision_ns'] >= final['decision_ns']:
            records[row['decision_ns']] = row
    return [records[key] for key in sorted(records)]
