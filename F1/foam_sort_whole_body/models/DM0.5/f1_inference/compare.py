"""Paired endpoint checks on a small, fixed subset of existing validation data.

This reads selected reader/source/image artifacts only. It never constructs a
model, opens ROS, publishes commands, ranks checkpoints or changes train data.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

import numpy as np

from f1_data.images import ImageLoader
from f1_data.reader import CAMERAS, Episode, require, sha256
from f1_training.data import verify_file
from f1_training.evaluation import physical_metrics
from f1_training.validation_protocol import describe_values, paired_seed
from .policy import TASK_ROOT

sys.path.insert(0, str(TASK_ROOT / 'robot'))
from f1_policy_client.observation import Observation
from f1_policy_client.policy import HttpPolicy


def finite_json(value):
    if isinstance(value, dict):
        return {str(k): finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [finite_json(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def write_json(path, value):
    with Path(path).open('x') as stream:
        json.dump(finite_json(value), stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def select_samples(report, count=12):
    """Known H20 chassis labels; balanced moving/stopped, one per recording.

    Only truth availability and movement fields are used. Prediction quality,
    IK success and checkpoint errors do not influence the sample selection.
    """
    require(type(count) is int and 2 <= count <= 16, 'comparison sample count must be in [2,16]')
    allowed = {(s['episode'], int(s['start'])) for s in report['selection']['samples']}
    candidates = {}
    for record in report['records']:
        key = record['episode'], int(record['start'])
        if key not in allowed or key in candidates:
            continue
        truth = record['metrics']['20']
        if truth['base_known_steps'] != 20:
            continue
        candidates[key] = {'episode': key[0], 'start': key[1],
                           'stratum': 'moving' if truth['moving_steps'] > 0 else 'stopped',
                           'h20_base_known_steps': 20, 'h20_base_moving_steps': truth['moving_steps']}
    pools = {name: sorted([v for v in candidates.values() if v['stratum'] == name],
                          key=lambda v: (v['episode'], v['start'])) for name in ('moving', 'stopped')}
    require(all(pools.values()), 'known-base Pass A selection must contain both moving and stopped windows')
    selected, used = [], set()
    for name, quota in (('moving', count//2), ('stopped', count-count//2)):
        pool = pools[name]
        # Deterministic equal-bin desired positions with nearest unused episode.
        for slot in range(quota):
            desired = min(len(pool)-1, (2*slot+1)*len(pool)//(2*quota))
            order = sorted(range(len(pool)), key=lambda index: (abs(index-desired), index))
            choice = next((pool[i] for i in order if pool[i]['episode'] not in used), None)
            require(choice is not None, f'not enough distinct known-base {name} recordings')
            selected.append(choice)
            used.add(choice['episode'])
    return sorted(selected, key=lambda sample: (sample['episode'], sample['start']))


def load_samples(release_path, reference_report, count):
    """Load only selected episodes and their original three compressed images."""
    release_path, reference_report = Path(release_path).resolve(), Path(reference_report).resolve()
    release, report = json.loads(release_path.read_text()), json.loads(reference_report.read_text())
    require(release.get('schema') == 'f1_dm05_release_v1' and release.get('training_approved') is True,
            'approved F1 release required')
    stats_path = verify_file(release['statistics'], release_path.parent)
    require(report.get('schema') == 'f1_generated_action_metrics_v2', 'existing validation report required')
    contract = report['comparison_contract']
    require(contract['release_sha256'] == sha256(release_path), 'reference report/release mismatch')
    require(contract['statistics_sha256'] == sha256(stats_path), 'reference report/statistics mismatch')
    choices = select_samples(report, count)
    entries = {entry['episode']: entry for entry in release['episodes']}
    images = ImageLoader((release_path.parent / release['image_root']).resolve(), training=False)
    loaded, evidence = [], []
    for choice in choices:
        entry = entries[choice['episode']]
        require(entry['split'] == 'validation', 'comparison must use inherited validation sources')
        paths = {key: verify_file(entry[key], release_path.parent) for key in
                 ('source', 'source_metadata', 'poses', 'pose_metadata', 'images')}
        ep = Episode(paths['source'], paths['poses'])
        require(ep.episode == choice['episode'] and ep.split == 'validation', 'source identity/split mismatch')
        require(ep.contract == release['contract'], 'reader/release contract mismatch')
        require(ep.starts.tolist() == entry['valid_starts'], 'frozen source starts changed')
        raw = ep.sample(choice['start'])
        require(raw['state_mask'].all() and raw['action_mask'][:20, :23].all(),
                'selected sample requires fully known state19 and H20 physical targets')
        image_directory = images.root / ep.episode.replace('/', '_')
        require(paths['images'] == image_directory/'manifest.json', 'release image path mismatch')
        images.raw_images(ep, raw['camera_source_indices'])  # Existing hashes + Pillow RGB decode checks.
        image_manifest = images.manifests[ep.episode]['images']
        payloads, image_records = {}, {}
        for camera, index in zip(CAMERAS, raw['camera_source_indices']):
            image_entry = image_manifest[camera][str(int(index))]
            path = image_directory/image_entry['path']
            payloads[camera] = path.read_bytes()
            image_records[camera] = {'path': str(path), 'sha256': image_entry['sha256'], 'bytes': len(payloads[camera])}
        obs = Observation(tuple(map(float, raw['state'])), tuple(map(float, ep.data['state'][choice['start'], 16:21])),
                          payloads, 1, {})
        item = {**choice, 'source_sha256': ep.source_sha256, 'pose_sha256': sha256(ep.pose_cache),
                'images': image_records, 'state19': obs.state19, 'source_time_ns': int(ep.data['time_ns'][choice['start']])}
        evidence.append(item)
        loaded.append({'selection': item, 'observation': obs, 'truth': raw['action'][:, :23],
                       'action_mask': raw['action_mask'], 'state_mask': raw['state_mask']})
    binding = {'release_sha256': sha256(release_path), 'statistics_sha256': sha256(stats_path)}
    return loaded, {'schema': 'f1_paired_endpoint_selection_v1', 'release': str(release_path), **binding,
                    'reference_report': str(reference_report), 'reference_report_sha256': sha256(reference_report),
                    'selection_method': 'Pass A known-state and H20-known-base; equal moving/stopped strata; equal-bin positions; distinct recordings; no prediction-error selection',
                    'state_origin': 'recorded arm/gripper feedback plus dataset reconstructed current base estimate; not live hardware',
                    'samples': evidence}


def prediction_metrics(response, sample):
    action = np.asarray(response.get('diagnostics', {}).get('physical_action23'), dtype=float)
    require(action.shape == (50, 23), 'server must return diagnostics.physical_action23 with H50/23D')
    metrics = physical_metrics(action, sample['truth'], sample['observation'].state19,
                               sample['action_mask'], sample['state_mask'])['20']
    diagnostics = {}
    for name, start, stop in (('left_gripper', 18, 19), ('right_gripper', 19, 20), ('wz', 22, 23)):
        values = action[:20, start:stop]
        known = values[np.isfinite(values)]
        diagnostics[name] = {'min': float(known.min()) if len(known) else None,
                             'max': float(known.max()) if len(known) else None,
                             'nonfinite_elements': int((~np.isfinite(values)).sum())}
        if name == 'wz':
            diagnostics[name].update(abs_max=float(np.abs(known).max()) if len(known) else None,
                                     nonzero_elements=int((known != 0).sum()))
        else:
            diagnostics[name]['outside_0_100_elements'] = int(((known < 0) | (known > 100)).sum())
    return finite_json(metrics), finite_json(diagnostics)


def evaluate_response(response, sample, health_binding, request_id, seed, stamp):
    require(response.get('request_id') == request_id, 'response/request identity mismatch')
    require(response.get('seed') == seed and response.get('observation_monotonic_ns') == stamp,
            'server seed/timestamp echo mismatch; deploy seeded server first')
    require(response.get('model_binding') == health_binding, 'model binding changed during comparison')
    require(response.get('state19') == list(sample['observation'].state19) and
            response.get('anchor_joints14_deg') == list(sample['observation'].anchor_joints14),
            'server state/FK anchor differs from the paired observation')
    require(response.get('status') in ('ready', 'rejected'), 'server returned no policy/IK result')
    metrics, bounds = prediction_metrics(response, sample)
    timing = response.get('diagnostics', {}).get('timing_seconds', {})
    require(all(isinstance(timing.get(key), (int, float)) and np.isfinite(timing[key]) and timing[key] >= 0
                for key in ('preprocess', 'model', 'ik')), 'server model/IK timing diagnostics missing')
    return {'status': response['status'], 'ik_rejected': response['status'] == 'rejected',
            'ik_reason': response.get('reason'), 'ik': response.get('ik'), 'h20': metrics,
            'h20_output_ranges': bounds, 'timing_seconds': dict(timing)}


def run_pair(clients, bindings, sample, seed, request_id, output):
    observed = replace(sample['observation'], captured_ns=time.monotonic_ns())
    started = time.monotonic()

    def request(label, client):
        begin = time.monotonic()
        try:
            response = client.infer(observed, request_id, seed=seed)
            elapsed = time.monotonic()-begin
            filename = f'{request_id}_{label}.json'
            write_json(output/filename, response)
            result = evaluate_response(response, sample, bindings[label], request_id, seed, observed.captured_ns)
            result['timing_seconds']['end_to_end'] = elapsed
            result['response_file'] = filename
            return result
        except Exception as exc:
            return {'status': 'request_error', 'error': f'{type(exc).__name__}: {exc}',
                    'timing_seconds': {'end_to_end': time.monotonic()-begin}}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {label: executor.submit(request, label, client) for label, client in clients.items()}
        results = {label: future.result() for label, future in futures.items()}
    return {'sample': {'episode': sample['selection']['episode'], 'start': sample['selection']['start'],
                       'stratum': sample['selection']['stratum']}, 'seed': seed,
            'observation_monotonic_ns': observed.captured_ns,
            'pair_wall_seconds': time.monotonic()-started, 'endpoints': results}


def summarize(records, labels):
    """Only measured records; warmup and transport failures stay visible."""
    summary = {}
    for label in labels:
        rows = [record['endpoints'][label] for record in records]
        valid = [row for row in rows if row['status'] != 'request_error']
        keys = sorted(set().union(*(row.get('h20', {}).keys() for row in valid)))
        scalars = [key for key in keys if all(not isinstance(row['h20'].get(key), (dict, list)) for row in valid)]
        summary[label] = {'measured_requests': len(rows), 'completed_policy_results': len(valid),
                          'request_errors': len(rows)-len(valid),
                          'ik_rejected': sum(row['ik_rejected'] for row in valid),
                          'timing_seconds': {key: describe_values([row.get('timing_seconds', {}).get(key) for row in rows])
                                             for key in ('preprocess', 'model', 'ik', 'end_to_end')},
                          'h20_window_mean': {key: describe_values([row['h20'].get(key) for row in valid]) for key in scalars},
                          'h20_ranges_per_window': [row['h20_output_ranges'] for row in valid]}
    left, right = labels
    paired = [record for record in records if all(record['endpoints'][name]['status'] != 'request_error' for name in labels)]
    deltas = {}
    if paired:
        for key in summary[left]['h20_window_mean']:
            values = []
            for row in paired:
                a, b = row['endpoints'][left]['h20'].get(key), row['endpoints'][right]['h20'].get(key)
                values.append(b-a if a is not None and b is not None else None)
            deltas[key] = describe_values(values)
    return {'endpoints': summary, 'paired_windows': len(paired),
            'mean_delta_direction': f'{right} minus {left}', 'paired_h20_deltas': deltas,
            'automatic_checkpoint_selection': False,
            'limitation': 'Small balanced known-base diagnostic sample; not full Pass A/B, live feedback, robot feasibility or closed-loop task success.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=TASK_ROOT/'results/formal_training_20260917/release_pass_a.json')
    parser.add_argument('--reference-report', type=Path, required=True)
    parser.add_argument('--endpoint', action='append', default=[], metavar='LABEL=URL')
    parser.add_argument('--samples', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--seed', type=int, default=20260818)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true', help='verify selected source/images and save selection without HTTP')
    args = parser.parse_args(argv)
    require(args.warmup >= 0 and 0 <= args.seed < 2**32 and np.isfinite(args.timeout) and args.timeout > 0,
            'invalid warmup/seed/timeout')
    endpoints = {}
    for value in args.endpoint:
        label, separator, endpoint = value.partition('=')
        require(separator and label and all(c.isalnum() or c in '_-' for c in label), 'endpoint requires safe LABEL=URL')
        require(label not in endpoints, 'duplicate endpoint label')
        endpoints[label] = endpoint
    require(args.prepare_only or len(endpoints) == 2 and len(set(endpoints.values())) == 2, 'exactly two distinct endpoints required')
    require(not args.output.exists(), 'comparison output exists; use a new run directory')
    samples, selection = load_samples(args.release, args.reference_report, args.samples)
    args.output.mkdir(parents=True)
    write_json(args.output/'selection.json', selection)
    if args.prepare_only:
        print(json.dumps({'status': 'prepared', 'samples': len(samples), 'selection': str(args.output/'selection.json')}, ensure_ascii=False))
        return
    clients = {label: HttpPolicy(endpoint, args.timeout) for label, endpoint in endpoints.items()}
    bindings = {label: client.health()['model_binding'] for label, client in clients.items()}
    for label, binding in bindings.items():
        require(all(binding.get(key) == selection[key] for key in ('release_sha256', 'statistics_sha256')),
                f'{label} server release/statistics mismatch')
    require(all(binding.get('checkpoint_identity') for binding in bindings.values()), 'both endpoints must identify their checkpoint')
    require(bindings[list(endpoints)[0]]['checkpoint_identity'] != bindings[list(endpoints)[1]]['checkpoint_identity'],
            'comparison endpoints identify the same checkpoint')
    responses = args.output/'responses'
    responses.mkdir()
    run_id = uuid.uuid4().hex[:12]
    report = {'schema': 'f1_paired_endpoint_comparison_v1', 'created_at': datetime.now(timezone.utc).isoformat(),
              'endpoints': endpoints, 'model_bindings': bindings, 'selection_sha256': sha256(args.output/'selection.json'),
              'base_seed': args.seed, 'seed_schedule': 'sha256_seed_episode_start_generation_v1',
              'command_arguments': list(sys.argv[1:] if argv is None else argv),
              'transport_timeout_seconds': args.timeout,
              'warmup_excluded_from_summary': True, 'warmup': [], 'measured': []}
    for phase, count in (('warmup', args.warmup), ('measured', len(samples))):
        for index in range(count):
            sample = samples[index % len(samples)]
            key = sample['selection']
            seed = paired_seed(args.seed, key['episode'], key['start'], 'generation')
            row = run_pair(clients, bindings, sample, seed, f'{run_id}_{phase}_{index:03d}', responses)
            report[phase].append(row)
            write_json(args.output/f'{phase}_{index:03d}.json', row)
            print(json.dumps({'phase': phase, 'index': index, 'sample': row['sample'],
                              'endpoints': {k: {'status': v['status'], 'timing_seconds': v['timing_seconds']}
                                            for k, v in row['endpoints'].items()}}, ensure_ascii=False), flush=True)
    report['summary'] = summarize(report['measured'], list(endpoints))
    write_json(args.output/'report.json', report)
    print(json.dumps({'report': str(args.output/'report.json'), 'paired_windows': report['summary']['paired_windows'],
                      'endpoints': {name: {key: summary[key] for key in
                                          ('measured_requests', 'request_errors', 'ik_rejected', 'timing_seconds')}
                                    for name, summary in report['summary']['endpoints'].items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
