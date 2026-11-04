"""Offline, reviewed HIL segments -> pinned LeRobot v3 + immutable provenance."""
import argparse
import bisect
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import numpy as np
from .config import DAGGER_ROOT
from .contract import CONTRACT, FEATURE_NAMES, CAMERAS, sha256
from .recording import CALLBACK_IDENTITY_SOURCE, audit, read_rows
from .payload_archive import read_payload

HUMAN_SOURCES = {'human_hold', 'human_arm', 'human_hand', 'human_arm_and_hand'}
RESAMPLING = 'causal_observation_hold_and_published_action_v2'


def input_invalid_intervals(events):
    """Conservative cut from last trusted receipt through explicit recovery.

    An unfinished pause excludes the tail. Legacy episodes without these
    events still use the existing state/epoch/publication boundaries.
    """
    intervals, start = [], None
    for event in events:
        if event.get('event') == 'rlt_episode_finish_requested':
            intervals.append((event['monotonic_ns'], None))
        elif event.get('event') == 'input_pause_started':
            value = event['invalid_from_monotonic_ns']
            start = value if start is None else min(start, value)
        elif event.get('event') in ('input_pause_ended', 'hold_to_hil') and start is not None:
            intervals.append((start, event['monotonic_ns']))
            start = None
    if start is not None:
        intervals.append((start, None))
    return intervals


def _resample_groups(groups, controls, times, fps, valid_control):
    """Fixed-rate samples, without future observations or compressed gaps.

    A held observation is explicitly identified, not a new camera exposure.
    Actions are sampled independently from the last submitted control at the
    grid time. The existing source boundaries and freshness limits still apply.
    """
    segments = []
    rejected = 0
    max_observation_age = int(round(1e9 / fps) * 1.5)
    for group in groups:
        observation_times = [row['reference_monotonic_ns'] for row in group]
        selected = []
        start, end = observation_times[0], observation_times[-1]
        step = 0
        while True:
            # Round each rational offset independently: adding round(1e9/fps)
            # accumulates drift even for ideal 30 Hz input.
            grid = start + (step * 1_000_000_000 + fps // 2) // fps
            if grid > end:
                break
            step += 1
            source = group[bisect.bisect_right(observation_times, grid) - 1]
            source_time = source['reference_monotonic_ns']
            index = bisect.bisect_right(times, grid) - 1
            begin = bisect.bisect_right(times, source_time) - 1
            control = controls[index] if index >= 0 else None
            valid = (control is not None and begin >= 0
                     and 0 <= grid - source_time <= max_observation_age
                     and 0 <= grid - control['monotonic_ns'] <= 50_000_000
                     and all(0 <= grid - t <= 250_000_000 for t, valid in zip(source['state_timestamps_ns'], source.get('state_valid_mask', [True]*16)) if valid)
                     and all(0 <= grid - f['host_received_monotonic_ns'] <= 200_000_000
                             for f in source['frames'])
                     and all(valid_control(c, source['intervention_id'], source['policy_epoch'])
                             for c in controls[begin:index + 1]))
            if not valid:
                rejected += 1
                if len(selected) >= 2:
                    segments.append(selected)
                selected = []
                continue
            selected.append(dict(source, reference_monotonic_ns=grid,
                source_observation_monotonic_ns=source_time,
                observation_age_ns=grid - source_time,
                observation_reused=bool(selected and selected[-1]['observation_id'] == source['observation_id']),
                resampling=RESAMPLING, grid_index=step - 1,
                command_monotonic_ns=control['monotonic_ns'],
                command_sequence=control['command_sequence'],
                control_stream_sequence=control['stream_sequence'],
                action=control['submitted'], action_source_timestamps_ns=control['action_source_timestamps_ns']))
        if len(selected) >= 2:
            segments.append(selected)
    return segments, rejected


def select_segments(run, review, *, fps=30):
    run = Path(run).resolve()
    report = audit(run)
    if not report['valid']:
        raise ValueError('raw audit failed: ' + '; '.join(report['errors']))
    if type(fps) is not int or not 1 <= fps <= 30:
        raise ValueError('export fps must be integer 1..30')
    if review.get('contract') != CONTRACT or review.get('manifest_sha256') != report['manifest_sha256']:
        raise ValueError('review belongs to another raw episode/version')
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        raise ValueError('human reviewer identity required')
    approved = review.get('accepted_interventions')
    if not isinstance(approved, list) or not approved or any(type(x) is not int or x < 1 for x in approved):
        raise ValueError('explicit accepted intervention IDs required')
    manifest = json.loads((run/'journal/manifest.json').read_text())
    masked = manifest.get('schema_version') == 4
    mask = [True]*14 + [False, True] if masked else [True]*16
    if manifest['binding'].get('purpose') in ('input_check','teleop_play'):
        raise ValueError('input checks and teleop play are never expert training data')
    if manifest['binding'].get('execution_mode') != 'powered':
        raise ValueError('shadow cannot become expert robot action data')
    payload = {row['observation_id']: row for row in read_rows(run/'payload/frames.jsonl')}
    controls = read_rows(run/'journal/control.jsonl')
    times = [row['monotonic_ns'] for row in controls]
    observations = read_rows(run/'journal/observations.jsonl')
    invalid_intervals = input_invalid_intervals(read_rows(run/'journal/events.jsonl'))
    def input_valid(start, end):
        return not any(end >= lo and (hi is None or start <= hi) for lo, hi in invalid_intervals)
    groups, group = [], []
    prior_control = None
    prior_time = None
    prior_callback_ids = None
    period = round(1e9/fps)
    def valid_control(row, intervention=None, epoch=None):
        return (input_valid(row['monotonic_ns'], row['monotonic_ns'])
                and row['hil_state'] == 'hil_latched' and row['source'] in HUMAN_SOURCES
                and row.get('action_valid_mask', [True]*16) == mask
                and row['intervention_id'] in approved and row['submitted'] is not None
                and row['publication_complete'] is True and row['execution_mode'] == 'powered'
                and (intervention is None or row['intervention_id'] == intervention)
                and (epoch is None or row['policy_epoch'] == epoch))
    def flush():
        nonlocal group
        if len(group) >= 2:
            groups.append(group)
        group = []
    rejected = 0
    for obs in observations:
        t = obs['available_monotonic_ns']
        if prior_time is not None and t <= prior_time:
            raise ValueError('observation time regressed')
        prior_time = t
        if obs.get('camera_identity_source') == CALLBACK_IDENTITY_SOURCE:
            callback_ids = tuple(frame['frame_id'] for frame in obs['frames'])
            if (obs['camera_discontinuity'] or (prior_callback_ids is not None
                    and any(new != old + 1 for new, old in zip(callback_ids, prior_callback_ids)))):
                flush()
                prior_control = None
            prior_callback_ids = callback_ids
        index = bisect.bisect_right(times, t)-1
        row = controls[index] if index >= 0 else None
        valid = (input_valid(min(f['host_received_monotonic_ns'] for f in obs['frames']), t)
                 and row is not None and valid_control(row) and obs['observation_id'] in payload
                 and obs['hil_state'] == 'hil_latched'
                 and obs['intervention_id'] == row['intervention_id'] and obs['policy_epoch'] == row['policy_epoch']
                 and 0 <= t-row['monotonic_ns'] <= 50_000_000
                 and obs.get('state_valid_mask', [True]*16) == mask
                 and all(0 <= t-stamp <= 250_000_000 for stamp, valid in zip(obs['state_timestamps_ns'], mask) if valid)
                 and all(0 <= t-f['host_received_monotonic_ns'] <= 200_000_000 for f in obs['frames']))
        if valid:
            # Include the command governing the earliest actual image receipt,
            # even when it preceded the capture thread's polling cycle. A first
            # post-START command cannot label a pre-START image as correction.
            begin = bisect.bisect_right(times, obs['capture_started_monotonic_ns'])-1
            valid = begin >= 0 and all(valid_control(c, row['intervention_id'], row['policy_epoch'])
                                      for c in controls[begin:index+1])
        if not valid:
            rejected += 1
            flush()
            prior_control = None
            continue
        if group:
            prior = group[-1]
            intervening = controls[prior_control+1:index+1]
            if (t-prior['reference_monotonic_ns'] > int(period*1.5)
                    or not input_valid(prior['reference_monotonic_ns'], t)
                    or not all(valid_control(c, row['intervention_id'], row['policy_epoch']) for c in intervening)
                    or prior['intervention_id'] != row['intervention_id'] or prior['policy_epoch'] != row['policy_epoch']):
                flush()
        item = dict(observation_id=obs['observation_id'], reference_monotonic_ns=t,
            command_sequence=row['command_sequence'], control_stream_sequence=row['stream_sequence'],
            intervention_id=row['intervention_id'], policy_epoch=row['policy_epoch'],
            state=obs['state16'], action=row['submitted'], state_timestamps_ns=obs['state_timestamps_ns'],
            state_valid_mask=mask, action_valid_mask=mask,
            action_source_timestamps_ns=row['action_source_timestamps_ns'], frames=payload[obs['observation_id']]['frames'])
        group.append(item)
        prior_control = index
    flush()
    segments, grid_rejections = _resample_groups(groups, controls, times, fps, valid_control)
    if not segments:
        raise ValueError('no reviewed, contiguous, causally aligned HIL segment with >=2 frames')
    rows = [row for segment in segments for row in segment]
    report.update(selected_frames=len(rows), selected_segments=len(segments),
                  input_invalid_intervals=invalid_intervals,
                  rejected_observations=rejected, reviewer=review['reviewer'], fps=fps,
                  resampling=RESAMPLING, source_contiguous_groups=len(groups),
                  resampling_rejected_grid_points=grid_rejections,
                  unique_source_observations=len({row['observation_id'] for row in rows}),
                  held_observation_samples=sum(row['observation_reused'] for row in rows),
                  max_observation_age_ns=max(row['observation_age_ns'] for row in rows),
                  alignment='host_monotonic_receipt_not_exposure_sync', training_ready=False)
    return segments, report, manifest


def pinned_writer(lerobot_root):
    root = Path(lerobot_root).resolve(strict=True)
    binding = json.loads((DAGGER_ROOT/'configs/f1_lerobot_binding.json').read_text())
    for relative, digest in binding['files'].items():
        if sha256(root/relative) != digest:
            raise ValueError('LeRobot writer version differs from pinned source: ' + relative)
    if 'lerobot' in sys.modules:
        module_path = Path(sys.modules['lerobot'].__file__).resolve()
        if root not in module_path.parents:
            raise ValueError('another LeRobot is already imported')
    sys.path.insert(0, str(root/'src'))
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    return LeRobotDataset, binding


def export_dataset(run, review_path, output, lerobot_root, *, fps=30, writer_class=None):
    run, output = Path(run).resolve(), Path(output).resolve()
    if output == run or run in output.parents:
        raise ValueError('export must be outside the immutable raw episode')
    partial = output.with_name(output.name+'.partial')
    if output.exists() or partial.exists():
        raise FileExistsError('refusing to overwrite an existing export/partial export')
    review = json.loads(Path(review_path).read_text())
    segments, report, manifest = select_segments(run, review, fps=fps)
    if writer_class is None:
        writer_class, binding = pinned_writer(lerobot_root)
    else:
        binding = {'test_writer': True}
    from PIL import Image
    features = {'observation.state': {'dtype':'float32','shape':(16,),'names':list(FEATURE_NAMES)},
                'action': {'dtype':'float32','shape':(16,),'names':list(FEATURE_NAMES)}}
    masked = manifest.get('schema_version') == 4
    if masked:
        for name in ('observation.state_valid_mask', 'action_valid_mask'):
            features[name] = dict(dtype='bool', shape=(16,), names=list(FEATURE_NAMES))
    first_frames = segments[0][0]['frames']
    for frame in first_frames:
        features['observation.images.'+frame['camera']] = dict(dtype='video', shape=(frame['height'],frame['width'],3), names=['height','width','channels'])
    output.parent.mkdir(parents=True, exist_ok=True)
    writer_options = {}
    if not binding.get('test_writer'):
        from lerobot.configs.video import RGBEncoderConfig
        writer_options['rgb_encoder'] = RGBEncoderConfig(vcodec='h264', crf=18, g=2)
        writer_options['encoder_threads'] = 1
    writer = writer_class.create(repo_id='local/f1_hgdagger', root=partial, fps=fps, features=features,
        robot_type='f1_09_upper16_omnipicker', use_videos=True, video_backend='pyav', image_writer_threads=2, **writer_options)
    provenance = []
    try:
        for episode, segment in enumerate(segments):
            for index, row in enumerate(segment):
                frame = {'observation.state':np.array(row['state'],dtype=np.float32),
                         'action':np.array(row['action'],dtype=np.float32), 'task':manifest['task_prompt']}
                if masked:
                    frame['observation.state_valid_mask'] = np.asarray(row['state_valid_mask'], dtype=bool)
                    frame['action_valid_mask'] = np.asarray(row['action_valid_mask'], dtype=bool)
                for image in row['frames']:
                    with Image.open(BytesIO(read_payload(run/'payload', image))) as im:
                        pixels = np.asarray(im.convert('RGB'))
                    key = 'observation.images.'+image['camera']
                    if tuple(pixels.shape) != features[key]['shape']:
                        raise ValueError('image size changed inside export')
                    frame[key] = pixels
                writer.add_frame(frame)
                provenance.append(dict(row, episode_index=episode, frame_index=index, train_mask=True))
            if binding.get('test_writer'):
                writer.save_episode()
            else:
                writer.save_episode(parallel_encoding=False)
        writer.finalize()
        with (partial/'f1_source.jsonl').open('x') as stream:
            for row in provenance:
                stream.write(json.dumps(row,allow_nan=False)+'\n')
        report.update(contract=CONTRACT, source_manifest_sha256=sha256(run/'journal/manifest.json'),
                      source_payload_index_sha256=sha256(run/'payload/frames.jsonl'),
                      review_sha256=sha256(review_path), writer_binding=binding,
                      source_basis='f1dagger_single_owner_publication',
                      hardware_mask_exported=masked,
                      training_input_contract='right_only_mask_required_v1' if masked else 'legacy_full16',
                      synthetic=manifest['binding'].get('synthetic') is True,
                      human_review_accepted=manifest['binding'].get('synthetic') is not True,
                      write_completed=True, structure_verified=False)
        (partial/'f1_audit.json').write_text(json.dumps(report,indent=2))
        (partial/'f1_review.json').write_text(json.dumps(review,indent=2))
        # Real writer readback is mandatory before publishing the export.
        if not binding.get('test_writer'):
            reader = writer_class(repo_id='local/f1_hgdagger', root=partial, video_backend='pyav')
            if len(reader) != len(provenance) or reader.num_episodes != len(segments) or reader.fps != fps:
                raise ValueError('LeRobot frame/episode/fps structure mismatch')
            for i, row in enumerate(provenance):
                sample = reader[i]
                if not np.allclose(np.asarray(sample['action']),row['action'],rtol=1e-6,atol=1e-5):
                    raise ValueError('LeRobot action readback differs from publication evidence')
                if not np.allclose(np.asarray(sample['observation.state']),row['state'],rtol=1e-6,atol=1e-5):
                    raise ValueError('LeRobot state readback differs from feedback')
                if masked and (not np.array_equal(np.asarray(sample['observation.state_valid_mask']), row['state_valid_mask'])
                               or not np.array_equal(np.asarray(sample['action_valid_mask']), row['action_valid_mask'])):
                    raise ValueError('LeRobot hardware validity mask roundtrip mismatch')
                for camera in CAMERAS:
                    pixels = np.asarray(sample['observation.images.'+camera])
                    h,w,_ = features['observation.images.'+camera]['shape']
                    if pixels.shape != (3,h,w) or not np.isfinite(pixels).all():
                        raise ValueError('video reader returned wrong dimensions/nonfinite pixels')
            report['structure_verified'] = True
            report['reader_roundtrip_verified'] = True
            report['training_ready'] = not report['synthetic']
            (partial/'f1_audit.json').write_text(json.dumps(report,indent=2))
        os.replace(partial, output)
    except BaseException:
        # Release writer threads even on partial failure; retain all evidence.
        try:
            writer.finalize()
        except Exception:
            pass
        raise
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('audit','review-template','export'))
    p.add_argument('run', type=Path)
    p.add_argument('--review', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--lerobot-root', default=os.environ.get('F1_LEROBOT_ROOT'))
    p.add_argument('--fps',type=int,default=30)
    args = p.parse_args()
    if args.mode == 'export':
        if args.review is None or args.output is None or not args.lerobot_root:
            p.error('export requires --review, --output and --lerobot-root')
        result = export_dataset(args.run,args.review,args.output,args.lerobot_root,fps=args.fps)
    else:
        result = audit(args.run)
        if args.mode == 'review-template':
            if args.output is None or not result['valid']:
                p.error('review-template requires a valid raw episode and --output')
            template = dict(contract=CONTRACT,manifest_sha256=result['manifest_sha256'],
                            reviewer='',accepted_interventions=[],notes='Review the raw images and command/state evidence before selecting IDs.')
            with args.output.open('x') as stream:
                json.dump(template,stream,indent=2)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
