#!/usr/bin/env python3
"""Build versioned 21D/19D candidate packs; preserve source and approval boundary."""
import argparse
import collections
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import yaml

TASK = Path(__file__).resolve().parents[1]


def dump(path, obj):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    temp.replace(path)


def runs(mask):
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def previous_sample(times, values, grid, max_age_ns):
    times = np.asarray(times, dtype=np.int64)
    values = np.asarray(values)
    if values.ndim == 1:
        values = values[:, None]
    if not len(times) or len(times) != len(values) or np.any(np.diff(times) <= 0):
        raise ValueError('Sample timestamps must be present, unique and increasing')
    indices = np.searchsorted(times, grid, side='right')-1
    safe = np.maximum(indices, 0)
    ages = grid-times[safe]
    fresh = (indices >= 0) & (ages >= 0) & (ages <= max_age_ns)
    valid = fresh[:, None] & np.isfinite(values[safe])
    sampled = values[safe].astype(np.float32)
    sampled[~valid] = np.nan
    return sampled, valid, indices, ages


def chunk_starts(valid, horizon):
    """No repeated last actions; no chunk crosses an invalid frame."""
    answer = np.zeros(len(valid), dtype=bool)
    if len(valid) >= horizon:
        counts = np.r_[0, np.cumsum(valid, dtype=np.int64)]
        answer[:len(valid)-horizon+1] = counts[horizon:]-counts[:-horizon] == horizon
    return answer


def gripper_pulses(times, values, start_ns, threshold=60, merge_gap_s=0.5):
    """Numeric excursions for review, not grasp/release/cycle classification."""
    low = np.asarray(values).reshape(-1) <= threshold
    groups = []
    for a, b in runs(low):
        begin, end = int(times[a]), int(times[b-1])
        if groups and begin-groups[-1][1] <= round(merge_gap_s*1e9):
            groups[-1][1] = end
        else:
            groups.append([begin, end])
    return [{'start_s': (a-start_ns)/1e9, 'end_s': (b-start_ns)/1e9,
             'meaning': f'raw_command_le_{threshold}_excursion_not_verified_release'} for a, b in groups]


def fingerprint(meta):
    fp = meta.get('source_fingerprint', {})
    return (meta.get('source_relative_path'),
            fp.get('size_bytes', fp.get('size', meta.get('source_size_bytes'))),
            fp.get('mtime_ns', meta.get('source_mtime_ns')))


def csv_write(path, rows):
    if not rows:
        path.write_text('')
        return
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k,v in row.items()})


def assemble(episode, root, source, cfg, audit, override, split):
    signal_json = root/'signals'/(episode+'.json')
    camera_json = root/'cameras'/(episode+'.json')
    sm, cm = json.loads(signal_json.read_text()), json.loads(camera_json.read_text())
    if any(meta.get('status')!='complete' or meta.get('episode')!=episode for meta in (sm,cm)):
        raise ValueError('Intermediate extraction incomplete or episode identity mismatch')
    signal_stat=signal_json.with_suffix('.npz').stat()
    if sm.get('npz_fingerprint') != {'size':signal_stat.st_size,'mtime_ns':signal_stat.st_mtime_ns}:
        raise ValueError('Signal NPZ changed after metadata commit')
    if hashlib.sha256(camera_json.with_suffix('.npz').read_bytes()).hexdigest()!=cm.get('npz_sha256'):
        raise ValueError('Camera NPZ checksum differs from committed metadata')
    if cm.get('run_config',{}).get('offset_deviation_limit_ns')!=round(cfg['camera']['header_offset_outlier_threshold_s']*1e9):
        raise ValueError('Camera candidate threshold differs from cleaning configuration')
    sfp, cfp = fingerprint(sm), fingerprint(cm)
    if sfp != cfp or any(x is None for x in sfp):
        raise ValueError(f'Signal/camera source fingerprints disagree: {sfp} {cfp}')
    raw = source/sfp[0]
    stat = raw.stat()
    if (stat.st_size, stat.st_mtime_ns) != sfp[1:]:
        raise ValueError('Source changed after intermediate extraction')
    if (sm['start_ns'], sm['end_ns']) != (cm['start_ns'], cm['end_ns']):
        raise ValueError('Source time bounds differ')
    start, end = int(sm['start_ns']), int(sm['end_ns'])
    hz, horizon = cfg['grid']['frequency_hz'], cfg['chunks']['horizon']
    count = int(math.floor((end-start)/1e9*hz))+1
    grid = start+np.rint(np.arange(count)*1e9/hz).astype(np.int64)
    grid = grid[grid <= end]
    max_signal_age = round(cfg['grid']['max_signal_age_s']*1e9)
    max_image_age = round(cfg['grid']['max_camera_log_age_s']*1e9)
    with np.load(signal_json.with_suffix('.npz'), allow_pickle=False) as s, np.load(camera_json.with_suffix('.npz'), allow_pickle=False) as c:
        hal, hal_valid, _, _ = previous_sample(s['hal_t_ns'], s['hal_q'], grid, max_signal_age)
        lead, lead_valid, _, _ = previous_sample(s['lead_t_ns'], s['lead_q'], grid, max_signal_age)
        if hal.shape[1] != 19 or lead.shape[1] != 14:
            raise ValueError('Unexpected joint dimensions')
        lc,lcv,_,_ = previous_sample(s['left_cmd_t_ns'],s['left_cmd'],grid,max_signal_age)
        rc,rcv,_,_ = previous_sample(s['right_cmd_t_ns'],s['right_cmd'],grid,max_signal_age)
        ls,lsv,_,_ = previous_sample(s['left_state_t_ns'],s['left_state'],grid,max_signal_age)
        rs,rsv,_,_ = previous_sample(s['right_state_t_ns'],s['right_state'],grid,max_signal_age)
        base,bv,_,_ = previous_sample(s['base_t_ns'],s['base_v'],grid,max_signal_age)
        if base.shape[1] != 3 or any(x.shape[1] != 1 for x in (lc,rc,ls,rs)):
            raise ValueError('Unexpected base or gripper dimensions')
        state = np.concatenate([hal[:,:14],ls,rs,hal[:,14:19]],axis=1)
        state_valid = np.concatenate([hal_valid[:,:14],lsv,rsv,hal_valid[:,14:19]],axis=1)
        action = np.concatenate([lead,lc,rc,base],axis=1)
        action_valid = np.concatenate([lead_valid,lcv,rcv,bv],axis=1)
        confirmed_bad_base = override.get('estimated_base_label_status') == 'requires_per_cycle_relabel_before_supervision'
        if confirmed_bad_base:
            action_valid[:,16:19] = False
        # Preserve suspect numeric targets for auditing; validity controls their use.
        image_indices, image_ages, image_valid = [], [], []
        for camera in ['head','left','right']:
            source_indices=np.flatnonzero(c[camera+'_timestamp_candidate_valid'])
            stamps = c[camera+'_log_ns'][source_indices]
            _, fresh, indices, ages = previous_sample(stamps, np.ones(len(stamps)), grid, max_image_age)
            valid = fresh[:,0]
            original_indices=np.where(indices>=0,source_indices[np.maximum(indices,0)],-1)
            image_indices.append(original_indices)
            image_ages.append(ages/1e9)
            image_valid.append(valid)
        image_indices = np.stack(image_indices,axis=1)
        image_ages = np.stack(image_ages,axis=1).astype(np.float32)
        image_valid = np.stack(image_valid,axis=1)
        signal_observation_valid = state_valid.all(axis=1)
        observation_valid = signal_observation_valid & image_valid.all(axis=1)
        technical_valid = observation_valid & action_valid.all(axis=1)
        future_signal_action_valid=signal_observation_valid & action_valid.all(axis=1)
        starts = chunk_starts(future_signal_action_valid,horizon) & image_valid.all(axis=1)
        gripper_cfg=cfg['gripper_review']
        pulses = gripper_pulses(s['left_cmd_t_ns'],s['left_cmd'],start,
                               gripper_cfg['raw_value_threshold'],gripper_cfg['merge_gap_s'])
    pack = root/'aligned'/episode
    pack.parent.mkdir(parents=True,exist_ok=True)
    with pack.with_suffix('.npz.tmp').open('wb') as f:
        np.savez_compressed(f, time_ns=grid, state=state, action=action,
            state_valid=state_valid, action_candidate_valid=action_valid,
            camera_source_indices=image_indices, camera_log_age_s=image_ages,
            camera_timestamp_candidate_valid=image_valid, observation_candidate_valid=observation_valid,
            joint_frame_candidate_valid=technical_valid, chunk_start_candidate_valid=starts,
            future_signal_action_candidate_valid=future_signal_action_valid,
            training_approved=np.asarray(False), source_relative_path=np.asarray(sfp[0]))
    pack.with_suffix('.npz.tmp').replace(pack.with_suffix('.npz'))
    windows = []
    for idx,(a,b) in enumerate(runs(future_signal_action_valid)):
        windows.append({'episode':episode,'window':idx,'source_start_frame':int(a),'source_stop_frame_exclusive':int(b),
            'start_s':float((grid[a]-start)/1e9),'last_frame_s':float((grid[b-1]-start)/1e9),
            'frames':int(b-a),'chunk_starts':int(starts[a:b].sum()),'split':split,
            'kind':'continuous_signal_action_window_observation_starts_masked_not_confirmed_cycle','training_approved':False})
    interior = [p for p in pulses if p['start_s'] >= gripper_cfg['ignore_start_before_s']]
    multiple_events = bool(len(interior)>=2 and interior[-1]['start_s']-interior[0]['start_s'] >= gripper_cfg['separated_event_span_s'])
    reasons = list(audit.get('reasons',[])) + list(override.get('reasons',[]))
    if multiple_events:
        reasons.append('multiple_separated_gripper_excursions_review_cycles_or_retries')
    if confirmed_bad_base:
        reasons.append('confirmed_bad_base_supervision_masked_until_relabel')
    result = {'episode':episode,'source_relative_path':sfp[0],'source_size_bytes':sfp[1],'source_mtime_ns':sfp[2],
        'signal_npz_fingerprint':sm['npz_fingerprint'],'camera_npz_sha256':cm['npz_sha256'],
        'start_ns':start,'end_ns':end,'grid_hz':hz,'frames':len(grid),'state_dim':21,'action_dim':19,
        'signal_observation_valid_frames':int(signal_observation_valid.sum()),
        'observation_candidate_valid_frames':int(observation_valid.sum()),
        'joint_frame_candidate_valid_frames':int(technical_valid.sum()),
        'chunk_horizon':horizon,'chunk_candidate_starts':int(starts.sum()),
        'continuous_windows':len(windows),'split':split,'confirmed_bad_base_masked':confirmed_bad_base,
        'gripper_excursions':pulses,'multiple_event_review_candidate':multiple_events,
        'observed_transport_cycles':override.get('observed_transport_cycles'),
        'review_override_evidence':override.get('evidence'),
        'cycle_boundaries_finalized':False,'review_reasons':sorted(set(reasons)),
        'training_approved':False,'note':'Candidate packs on log time; no exposure calibration, task-success approval or normalization.'}
    dump(pack.with_suffix('.json'),result)
    return result, windows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=TASK/'configs/cleaning_v1.yaml')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--episode',action='append')
    args=parser.parse_args()
    cfg=yaml.safe_load(args.config.read_text())
    root=args.output or TASK/cfg['output'];root.mkdir(parents=True,exist_ok=True)
    source=Path(cfg['source']);audit_root=TASK/cfg['source_audit']
    audit={r['episode']:r for r in map(json.loads,(audit_root/'screening/review_manifest.jsonl').read_text().splitlines())}
    original={str(Path(r['relative_path']).parent):r for r in map(json.loads,(audit_root/'mcap_audit/episodes.jsonl').read_text().splitlines())}
    override_text=(TASK/'configs/review_overrides.yaml').read_text()
    overrides=yaml.safe_load(override_text).get('episodes',{})
    groups=collections.defaultdict(list)
    for episode,row in original.items():groups[episode.split('/')[0]].append((row['start_ns'],episode))
    splits={}
    for group in groups.values():
        ordered=sorted(group);nval=max(1,math.ceil(len(ordered)*cfg['split']['validation_fraction']))
        for i,(_,episode) in enumerate(ordered):splits[episode]='validation' if i>=len(ordered)-nval else 'train'
    dump(root/'source_split.json',{'unit':'original_recording','child_cycles_share_parent_split':True,'assignments':splits})
    (root/'config_snapshot.yaml').write_text(args.config.read_text())
    (root/'review_overrides_snapshot.yaml').write_text(override_text)
    selected=sorted(args.episode or audit)
    if set(selected)-set(audit):raise ValueError('Unknown episode requested')
    results,windows,errors=[],[],[]
    for i,episode in enumerate(selected):
        try:
            row,rows=assemble(episode,root,source,cfg,audit[episode],overrides.get(episode,{}),splits[episode])
            results.append(row);windows.extend(rows)
        except Exception as exc:
            errors.append({'episode':episode,'type':type(exc).__name__,'error':str(exc)})
        if (i+1)%50==0 or i+1==len(selected):print(f'Assembled {i+1}/{len(selected)}; errors={len(errors)}',flush=True)
    with (root/'episode_manifest.jsonl').open('w') as f:
        for row in results:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    csv_write(root/'technical_windows.csv',windows)
    review=[{k:r[k] for k in ['episode','split','frames','joint_frame_candidate_valid_frames','chunk_candidate_starts',
        'confirmed_bad_base_masked','multiple_event_review_candidate','observed_transport_cycles','gripper_excursions','review_reasons','training_approved']} for r in results]
    csv_write(root/'review_queue.csv',review)
    dump(root/'errors.json',errors)
    summary={'version':cfg['version'],'generated_at':datetime.now(timezone.utc).isoformat(),
        'config_sha256':hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'review_overrides_sha256':hashlib.sha256(override_text.encode()).hexdigest(),
        'requested_episodes':len(selected),'assembled_episodes':len(results),'errors':len(errors),
        'frames':sum(r['frames'] for r in results),'technical_joint_frames':sum(r['joint_frame_candidate_valid_frames'] for r in results),
        'technical_chunk_starts':sum(r['chunk_candidate_starts'] for r in results),
        'technical_continuous_windows':len(windows),
        'known_bad_base_episodes_masked':sum(r['confirmed_bad_base_masked'] for r in results),
        'multiple_gripper_event_review_candidates':sum(r['multiple_event_review_candidate'] for r in results),
        'split_counts':dict(collections.Counter(r['split'] for r in results)),
        'approved_training_episodes':0,'raw_files_modified':0,
        'meaning':'Technical candidates only; unknown visual outcomes/cycle cuts and label accuracy are not automatically approved.'}
    dump(root/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))
    if errors:raise SystemExit(1)


if __name__=='__main__':main()
