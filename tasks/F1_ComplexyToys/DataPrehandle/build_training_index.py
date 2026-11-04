#!/usr/bin/env python3
"""Audited F1 raw data -> offline anchor/action JSONL candidate, never training approval.

This index keeps the baseline whole-observation selection and independently
validates a complete future published-action window. It does not write LeRobot,
decode/re-pair images, run inference, or modify raw recordings. Time limits are
explicit nanoseconds, independent of FPS. All outputs remain training_ready=false.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys

PROJECTS = Path(__file__).resolve().parents[3]
for _source in (PROJECTS / 'DAgger/F1/src', PROJECTS / 'DAgger/shared/src'):
    sys.path.insert(0, str(_source))

from f1dagger.contract import CONTRACT, CAMERAS, sha256
from f1dagger.export import input_invalid_intervals
from f1dagger.recording import audit, read_rows

SCHEMA = 'f1_anchor_action_index_v1'
CONFIG_SCHEMA = 'f1_anchor_action_index_config_v1'
HUMAN_SOURCES = frozenset(('human_hold', 'human_arm', 'human_hand', 'human_arm_and_hand'))
HOME_ENDS = frozenset(('home_completed_feedback', 'home_cancelled', 'home_stopped'))


@dataclass(frozen=True)
class Config:
    fps: int = 30
    horizon: int = 50
    required_cameras: tuple[str, ...] = ('head', 'right_wrist')
    max_observation_age_ns: int = 50_000_000
    max_camera_age_ns: int = 200_000_000
    max_state_age_ns: int = 250_000_000
    max_command_age_ns: int = 50_000_000
    max_control_gap_ns: int = 50_000_000
    allow_partial_windows: bool = False
    min_action_steps: int = 1

    def __post_init__(self):
        if type(self.fps) is not int or not 1 <= self.fps <= 30:
            raise ValueError('fps must be integer 1..30')
        if type(self.horizon) is not int or self.horizon < 1:
            raise ValueError('horizon must be a positive integer')
        if (type(self.allow_partial_windows) is not bool or type(self.min_action_steps) is not int
                or not 1 <= self.min_action_steps <= self.horizon):
            raise ValueError('Invalid partial-window configuration')
        if (not self.required_cameras or len(set(self.required_cameras)) != len(self.required_cameras)
                or not set(self.required_cameras).issubset(CAMERAS)):
            raise ValueError('required_cameras must be a nonempty unique subset of F1 cameras')
        for key, value in asdict(self).items():
            if key.startswith('max_') and (type(value) is not int or value <= 0):
                raise ValueError(f'{key} must be positive integer nanoseconds')


def load_config(path: Path | None, *, fps=None, horizon=None) -> tuple[Config, dict]:
    document = json.loads(path.read_text()) if path is not None else {'schema': CONFIG_SCHEMA}
    if document.get('schema') != CONFIG_SCHEMA:
        raise ValueError('unsupported config schema')
    unknown = set(document) - set(Config.__dataclass_fields__) - {'schema', 'metadata'}
    if unknown:
        raise ValueError('unknown config fields: ' + ', '.join(sorted(unknown)))
    values = {key: value for key, value in document.items() if key in Config.__dataclass_fields__}
    if 'required_cameras' in values:
        values['required_cameras'] = tuple(values['required_cameras'])
    config = Config(**values)
    if fps is not None:
        config = replace(config, fps=fps)
    if horizon is not None:
        config = replace(config, horizon=horizon)
    return config, document


def grid_time(origin: int, index: int, fps: int) -> int:
    """Round the rational offset independently, with no accumulated ns drift."""
    return origin + (index * 1_000_000_000 + fps // 2) // fps


def _vector_ok(value):
    return isinstance(value, list) and len(value) == 16 and all(
        type(v) in (int, float) and math.isfinite(v) for v in value)


def _hardware_mask(row, key):
    mask = row.get(key, [True]*16)
    if (not isinstance(mask, list) or any(type(v) is not bool for v in mask)
            or mask not in ([True]*16, [True]*14+[False, True])):
        raise ValueError('Unsupported physical validity mask')
    return mask


def _control_reason(row, *, episode_id, accepted_interventions, intervention, epoch, allow_home=False):
    if row.get('episode_id') != episode_id:
        return 'control_episode_changed'
    if row.get('hil_state') != 'hil_latched':
        return 'control_not_hil_latched'
    if row.get('source') not in HUMAN_SOURCES and not (allow_home and row.get('source') == 'home'):
        return 'control_not_human_source'
    if row.get('intervention_id') not in accepted_interventions:
        return 'control_intervention_not_selected'
    if row.get('intervention_id') != intervention:
        return 'control_intervention_changed'
    if row.get('policy_epoch') != epoch:
        return 'control_epoch_changed'
    if row.get('execution_mode') != 'powered':
        return 'control_not_powered'
    if row.get('publication_complete') is not True or row.get('submitted') is None:
        return 'control_publication_incomplete'
    if not _vector_ok(row['submitted']):
        return 'control_action_invalid'
    stamps = row.get('action_source_timestamps_ns', [])
    mask = _hardware_mask(row, 'action_valid_mask')
    if (len(stamps) != 16 or any(type(t) is not int or not 0 < t <= row['monotonic_ns']
                                for t, valid in zip(stamps, mask) if valid)
            or any(t not in (None, 0) or row['submitted'][i] != 0 for i, (t, valid) in enumerate(zip(stamps, mask)) if not valid)):
        return 'control_action_provenance_invalid'
    # A gripper value can legally remain held without a new per-field publication.
    # Never impose command-age limits separately on these per-field timestamps.
    return None


def _event_intervals(events, episode_end):
    """Home blocks its whole duration; unmatched Home start blocks to EOF."""
    intervals, active = [], None
    for event in sorted(events, key=lambda row: row['monotonic_ns']):
        name, t = event['event'], event['monotonic_ns']
        if name == 'home_started' and active is None:
            active = t
        elif name in HOME_ENDS and active is not None:
            intervals.append((active, t))
            active = None
    if active is not None:
        intervals.append((active, episode_end))
    return intervals


def select_windows(observations, controls, payload, events, *, episode_id,
                   config=Config(), accepted_interventions, accepted_time_ranges_ns=None, accepted_home_intervals_ns=()):
    """Pure selection API; production callers must first pass immutable raw audit.

    Returns (anchors, rejections, summary). Rejection records distinguish input
    from labels. No future images are required after a valid anchor. Source
    lists/dictionaries are never mutated. Ranges are inclusive monotonic ns.
    """
    if not observations or not controls:
        raise ValueError('observations and controls must be nonempty')
    obs_times = [r['available_monotonic_ns'] for r in observations]
    times = [r['monotonic_ns'] for r in controls]
    if any(b <= a for a, b in zip(obs_times, obs_times[1:])):
        raise ValueError('observation available times must be strictly increasing')
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('control completion times must be strictly increasing')
    if any(r.get('episode_id') != episode_id for r in [*observations, *controls, *events]):
        raise ValueError('mixed source episode identities')
    approved = set(accepted_interventions)
    if not approved or any(type(i) is not int or i < 1 for i in approved):
        raise ValueError('nonempty positive intervention IDs required')
    if accepted_time_ranges_ns is not None:
        for pair in accepted_time_ranges_ns:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(type(t) is not int or t <= 0 for t in pair) or pair[0] > pair[1]):
                raise ValueError('accepted_time_ranges_ns must contain [start_ns,end_ns] pairs')
        if not accepted_time_ranges_ns:
            raise ValueError('reviewed time ranges must be nonempty')
    payload_by_id = {row['observation_id']: row for row in payload}
    origin = obs_times[0]
    start_events = [e['monotonic_ns'] for e in events if e['event'] == 'demonstration_started']
    stop_events = [e['monotonic_ns'] for e in events if e['event'] == 'demonstration_stopped']
    episode_start = min(start_events) if start_events else times[0]
    # Last observed command is the latest supported label time, never a license
    # to extrapolate its hold beyond the actual recorded episode.
    episode_end = min(times[-1], min(stop_events)) if stop_events else times[-1]
    home_intervals = _event_intervals(events, episode_end)
    # A watchdog is detected after the last trusted operator sample. State and
    # epoch changes alone cannot exclude that earlier stale-control interval.
    # Reuse the raw export boundary: unfinished pauses exclude the entire tail.
    input_intervals = input_invalid_intervals(sorted(events, key=lambda row: row['monotonic_ns']))

    def input_interval_invalid(begin_t, end_t):
        return any(end_t >= start and (stop is None or begin_t <= stop)
                   for start, stop in input_intervals)

    # Explicit allowlist only: a recorded V1 start and feedback-completed end.
    completed = []
    active = None
    for event in sorted(events, key=lambda r: r['monotonic_ns']):
        if event['event'] == 'home_started':
            active = event
        elif event['event'] in HOME_ENDS and active is not None:
            if (event['event'] == 'home_completed_feedback'
                    and active.get('preset') == '积木demo-V1'):
                completed.append((active['monotonic_ns'], event['monotonic_ns']))
            active = None
    accepted_home = [tuple(pair) for pair in accepted_home_intervals_ns]
    if len(set(accepted_home)) != len(accepted_home) or any(pair not in completed for pair in accepted_home):
        raise ValueError('Home allowlist must match recorded V1 feedback-completed intervals')

    end = min(obs_times[-1], episode_end)
    anchors, rejections = [], []
    grid_index = 0

    def reject(stage, reason, **details):
        rejections.append(dict(grid_index=grid_index, anchor_monotonic_ns=t,
                               stage=stage, reason=reason, **details))

    def validate_interval(begin_t, end_t, intervention, epoch):
        if input_interval_invalid(begin_t, end_t):
            return 'input_pause_interval'
        first = bisect.bisect_right(times, begin_t) - 1
        last = bisect.bisect_right(times, end_t) - 1
        if first < 0:
            return 'control_missing_at_interval_start'
        if begin_t - times[first] > config.max_command_age_ns:
            return 'control_stale_at_interval_start'
        for index in range(first, last + 1):
            row = controls[index]
            reason = _control_reason(row, episode_id=episode_id, accepted_interventions=approved,
                                     intervention=intervention, epoch=epoch,
                                     allow_home=any(a <= row["monotonic_ns"] <= b for a, b in accepted_home))
            if reason:
                return reason
            if index > first:
                if times[index] - times[index - 1] > config.max_control_gap_ns:
                    return 'control_gap'
                if row['command_sequence'] != controls[index - 1]['command_sequence'] + 1:
                    return 'control_sequence_gap'
        if end_t - times[last] > config.max_command_age_ns:
            return 'control_stale_at_interval_end'
        if any(a <= end_t and b >= begin_t and (a, b) not in accepted_home for a, b in home_intervals):
            return 'home_interval'
        return None

    while (t := grid_time(origin, grid_index, config.fps)) <= end:
        source = observations[bisect.bisect_right(obs_times, t) - 1]
        source_t = source['available_monotonic_ns']
        oid = source['observation_id']
        intervention, epoch = source['intervention_id'], source['policy_epoch']
        capture_start = source['capture_started_monotonic_ns']
        input_reason = None
        if not episode_start <= capture_start <= source_t <= t:
            input_reason = 'observation_outside_episode_or_noncausal'
        elif source.get('hil_state') != 'hil_latched' or intervention not in approved:
            input_reason = 'observation_not_selected_hil'
        elif t - source_t > config.max_observation_age_ns:
            input_reason = 'observation_stale'
        elif oid not in payload_by_id:
            input_reason = 'observation_payload_missing'
        elif not _vector_ok(source.get('state16')):
            input_reason = 'state_invalid'
        elif (len(source['state_timestamps_ns']) != 16
              or any(type(stamp) is not int or not 0 <= t - stamp <= config.max_state_age_ns
                     for stamp, valid in zip(source['state_timestamps_ns'], _hardware_mask(source, 'state_valid_mask')) if valid)
              or any(stamp not in (None, 0) or source['state16'][i] != 0
                     for i, (stamp, valid) in enumerate(zip(source['state_timestamps_ns'], _hardware_mask(source, 'state_valid_mask'))) if not valid)):
            input_reason = 'state_stale_or_future'
        else:
            frames = {f['camera']: f for f in payload_by_id[oid]['frames']}
            if any(camera not in frames for camera in config.required_cameras):
                input_reason = 'required_camera_missing'
            elif any(not 0 <= t - frames[c]['host_received_monotonic_ns'] <= config.max_camera_age_ns
                     for c in config.required_cameras):
                input_reason = 'required_camera_stale_or_future'
            elif input_interval_invalid(min(capture_start, *(frames[c]['host_received_monotonic_ns']
                                                             for c in config.required_cameras)), t):
                # A post-resume poll cannot relabel an actual image received
                # during the pause as a fresh expert observation.
                input_reason = 'input_pause_interval'
        if input_reason is None:
            input_reason = validate_interval(capture_start, t, intervention, epoch)
        if input_reason:
            reject('input', input_reason, observation_id=oid)
            grid_index += 1
            continue
        target_times = [grid_time(origin, grid_index + i, config.fps) for i in range(config.horizon)]
        truncation_reason = None
        if config.allow_partial_windows:
            valid_times = []
            for target in target_times:
                reason = 'incomplete_future_window' if target > episode_end else None
                if reason is None and accepted_time_ranges_ns is not None and not any(
                        start <= capture_start and target <= stop for start, stop in accepted_time_ranges_ns):
                    reason = 'outside_single_reviewed_time_range'
                if reason is None:
                    reason = validate_interval(valid_times[-1] if valid_times else t, target, intervention, epoch)
                if reason:
                    truncation_reason = reason
                    break  # Never reconnect supervision after a boundary or missing command.
                valid_times.append(target)
            if len(valid_times) < config.min_action_steps:
                reject('labels', truncation_reason or 'too_short_valid_prefix', observation_id=oid)
                grid_index += 1
                continue
            target_times = valid_times
        else:
            last_t = target_times[-1]
            if last_t > episode_end:
                reject('labels', 'incomplete_future_window', observation_id=oid)
                grid_index += 1
                continue
            if accepted_time_ranges_ns is not None and not any(
                    start <= capture_start and last_t <= stop for start, stop in accepted_time_ranges_ns):
                reject('review', 'outside_single_reviewed_time_range', observation_id=oid)
                grid_index += 1
                continue
            reason = validate_interval(t, last_t, intervention, epoch)
            if reason:
                reject('labels', reason, observation_id=oid)
                grid_index += 1
                continue
        selected_controls = [controls[bisect.bisect_right(times, target) - 1] for target in target_times]
        if any(target - c['monotonic_ns'] > config.max_command_age_ns
               for target, c in zip(target_times, selected_controls)):
            reject('labels', 'command_stale_at_action_grid', observation_id=oid)
            grid_index += 1
            continue
        actions = [dict(grid_monotonic_ns=target, command_monotonic_ns=c['monotonic_ns'],
                        command_age_ns=target - c['monotonic_ns'], command_sequence=c['command_sequence'],
                        control_stream_sequence=c['stream_sequence'], source=c['source'],
                        action=c['submitted'], action_source_timestamps_ns=c['action_source_timestamps_ns'],
                        action_valid_mask=_hardware_mask(c, 'action_valid_mask'))
                   for target, c in zip(target_times, selected_controls)]
        frames = payload_by_id[oid]['frames']
        anchors.append(dict(schema=SCHEMA, episode_id=episode_id, grid_index=grid_index,
            anchor_monotonic_ns=t, observation_id=oid, source_observation_monotonic_ns=source_t,
            observation_age_ns=t - source_t, capture_started_monotonic_ns=capture_start,
            intervention_id=intervention, policy_epoch=epoch, state=source['state16'],
            state_timestamps_ns=source['state_timestamps_ns'],
            state_valid_mask=_hardware_mask(source, 'state_valid_mask'),
            state_ages_ns=[t - stamp if valid else None for stamp, valid in zip(source['state_timestamps_ns'], _hardware_mask(source, 'state_valid_mask'))],
            frames=frames, camera_ages_ns={f['camera']: t - f['host_received_monotonic_ns'] for f in frames},
            image_mask={camera: camera in config.required_cameras for camera in CAMERAS},
            camera_discontinuity=source.get('camera_discontinuity'),
            camera_drop_counts=source.get('camera_drop_counts'),
            camera_identity_source=source.get('camera_identity_source'),
            action_window=actions, action_is_pad=[False] * len(actions) + [True] * (config.horizon-len(actions)),
            valid_action_steps=len(actions), truncation_reason=truncation_reason,
            supervision_phase='home' if actions[0]['source'] == 'home' else 'human',
            contains_home=any(a['source'] == 'home' for a in actions),
            alignment='host_monotonic_receipt_not_exposure_sync', training_ready=False))
        grid_index += 1
    summary = dict(grid_points=grid_index, retained_windows=len(anchors),
                   complete_action_windows=sum(len(r["action_window"]) == config.horizon for r in anchors),
                   partial_action_windows=sum(len(r["action_window"]) < config.horizon for r in anchors),
                   home_anchor_windows=sum(r["supervision_phase"] == "home" for r in anchors),
                   windows_containing_home=sum(r["contains_home"] for r in anchors),
                   unique_anchor_observations=len({row['observation_id'] for row in anchors}),
                   rejected_grid_points=len(rejections),
                   rejection_counts=dict(Counter(f"{r['stage']}:{r['reason']}" for r in rejections)),
                   grid_origin_monotonic_ns=origin, episode_start_monotonic_ns=episode_start,
                   supported_label_end_monotonic_ns=episode_end, raw_observations=len(observations),
                   raw_controls=len(controls), home_intervals_ns=home_intervals,
                   input_invalid_intervals_ns=input_intervals)
    return anchors, rejections, summary


def _validate_review(review, manifest_digest):
    if review.get('contract') != CONTRACT or review.get('manifest_sha256') != manifest_digest:
        raise ValueError('review belongs to another raw episode/version')
    if review.get('human_review_accepted') is not True:
        raise ValueError('explicit human_review_accepted=true required in reviewed mode')
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        raise ValueError('reviewer identity required')
    if not isinstance(review.get('accepted_interventions'), list):
        raise ValueError('accepted_interventions list required')
    if not isinstance(review.get('accepted_time_ranges_ns'), list) or not review['accepted_time_ranges_ns']:
        raise ValueError('explicit accepted_time_ranges_ns required; intervention-only review is insufficient')


def build_index(raw: Path, output: Path, *, mode: str, config=Config(), review_path=None,
                config_document=None, home_review=None):
    """Audit raw evidence, select windows, atomically publish a new candidate directory."""
    raw, output = Path(raw).resolve(), Path(output).resolve()
    if mode not in ('technical', 'reviewed'):
        raise ValueError('mode must be explicitly technical or reviewed')
    if raw == output or raw in output.parents:
        raise ValueError('output must be outside immutable raw episode')
    partial = output.with_name(output.name + '.partial')
    if output.exists() or partial.exists():
        raise FileExistsError('refusing to overwrite output or partial candidate')
    initial = audit(raw)
    if not initial['valid']:
        raise ValueError('raw audit failed: ' + '; '.join(initial['errors']))
    manifest = json.loads((raw / 'journal/manifest.json').read_text())
    if manifest['binding'].get('execution_mode') != 'powered':
        raise ValueError('shadow has no real powered action labels')
    if manifest['binding'].get('purpose') in ('input_check', 'teleop_play'):
        raise ValueError('input check and teleop play are not demonstration candidates')
    observations, controls, events = [read_rows(raw / 'journal' / (name + '.jsonl'))
                                      for name in ('observations', 'control', 'events')]
    payload = read_rows(raw / 'payload/frames.jsonl')
    if mode == 'reviewed':
        if review_path is None:
            raise ValueError('--review required in reviewed mode')
        review = json.loads(Path(review_path).read_text())
        _validate_review(review, initial['manifest_sha256'])
        accepted, ranges = review['accepted_interventions'], review['accepted_time_ranges_ns']
    else:
        if review_path is not None:
            raise ValueError('technical mode does not consume or create human approval')
        review = None
        accepted = sorted({c['intervention_id'] for c in controls if c['intervention_id'] > 0})
        ranges = None
    accepted_home = []
    if home_review is not None:
        if home_review.get('source_manifest_sha256') != initial['manifest_sha256']:
            raise ValueError('Home review belongs to another source version')
        accepted_home = home_review['accepted_intervals_ns']
    anchors, rejections, summary = select_windows(observations, controls, payload, events,
        episode_id=manifest['episode_id'], config=config, accepted_interventions=accepted,
        accepted_time_ranges_ns=ranges, accepted_home_intervals_ns=accepted_home)
    final = audit(raw)
    if not final['valid'] or final['manifest_sha256'] != initial['manifest_sha256']:
        raise ValueError('raw changed or failed re-audit while selecting candidate')
    report = dict(schema=SCHEMA, created_utc=datetime.now(timezone.utc).isoformat(),
        mode=mode, source_run=str(raw), source_episode_id=manifest['episode_id'],
        source_manifest_sha256=initial['manifest_sha256'], source_stream_sha256=manifest['stream_sha256'],
        source_payload_index_sha256=sha256(raw / 'payload/frames.jsonl'),
        builder_sha256=sha256(Path(__file__)),
        source_raw_unchanged=True, raw_audit_valid=True, action_contract=manifest['action_contract'],
        config=asdict(config), source_config_document=config_document,
        selected_interventions=accepted, accepted_time_ranges_ns=ranges,
        human_review_accepted=mode == 'reviewed',
        reviewer=review['reviewer'] if review else None,
        review_sha256=sha256(Path(review_path)) if review else None,
        training_ready=False, rlinf_reader_verified=False, lerobot_export=False,
        observation_selection='latest_past_whole_observation_baseline',
        alignment='host_monotonic_receipt_not_exposure_sync',
        padding='masked_prefix_no_synthetic_labels' if config.allow_partial_windows else 'none_complete_actual_action_windows_only',
        home_review=home_review,
        source_task_prompt=manifest.get('task_prompt'),
        task_success_reviewed=False if review is None else review.get('task_success') is True,
        limitations=['camera exposure clock mapping and latency not certified',
                     'whole-observation baseline retained; no per-camera re-pairing',
                     'ROI, normalization, F1 model adapter and inference timing not implemented here',
                     'JSONL technical index is not an RLinf/LeRobot training dataset',
                     'thresholds are explicit baseline choices, not validated task optima'],
        summary=summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial.mkdir()
    for name, rows in (('anchors.jsonl', anchors), ('rejections.jsonl', rejections)):
        with (partial / name).open('x') as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    report['output_sha256'] = {name: sha256(partial / name) for name in ('anchors.jsonl', 'rejections.jsonl')}
    (partial / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    os.replace(partial, output)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw', type=Path)
    parser.add_argument('--output', required=True, type=Path, help='new directory outside raw episode')
    parser.add_argument('--mode', required=True, choices=('technical', 'reviewed'))
    parser.add_argument('--review', type=Path, help='external manifest-bound human review; reviewed mode only')
    default_config = Path(__file__).resolve().parents[1] / 'configs/preprocess_baseline.json'
    parser.add_argument('--config', type=Path, default=default_config if default_config.exists() else None)
    parser.add_argument('--fps', type=int, help='override sample rate only, not any quality threshold')
    parser.add_argument('--horizon', type=int, help='number of real action points; default 50, no padding')
    args = parser.parse_args(argv)
    config, document = load_config(args.config, fps=args.fps, horizon=args.horizon)
    report = build_index(args.raw, args.output, mode=args.mode, config=config,
                         review_path=args.review, config_document=document)
    print(json.dumps(dict(output=str(args.output.resolve()), **report['summary'],
                          human_review_accepted=report['human_review_accepted'], training_ready=False),
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
