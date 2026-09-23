#!/usr/bin/env python3
"""Read-only MCAP audit: full non-image CDR decoding, camera message indexes.

Requires mcap, mcap-ros2-support, numpy and PyYAML. Does not mutate source data.
Image payloads and camera header/publish timestamps are intentionally not audited.
Numeric range thresholds are review hints, never robot joint/speed safety limits.
"""
from __future__ import annotations

import argparse
import csv
import json
import struct
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml
from mcap.data_stream import ReadDataStream
from mcap.reader import make_reader
from mcap.records import MessageIndex
from mcap_ros2.decoder import DecoderFactory


def timing(values):
    a = np.asarray(values, dtype=np.int64)
    if not len(a):
        return {"count": 0}
    d = np.diff(a) / 1e9
    pos = d[d > 0]
    median = float(np.median(pos)) if len(pos) else None
    gap_threshold = max(0.1, 3 * median) if median is not None else 0.1
    return {
        "count": len(a), "first_ns": int(a[0]), "last_ns": int(a[-1]),
        "zero_timestamps": int(np.sum(a == 0)),
        "backward_steps": int(np.sum(d < 0)), "duplicate_steps": int(np.sum(d == 0)),
        "median_positive_interval_s": median,
        "max_gap_s": float(np.max(d)) if len(d) else None,
        "p99_positive_interval_s": float(np.quantile(pos, 0.99)) if len(pos) else None,
        "gap_threshold_s": gap_threshold, "large_gap_count": int(np.sum(d > gap_threshold)),
        "rate_over_span_hz": float((len(a)-1)/((a[-1]-a[0])/1e9)) if len(a)>1 and a[-1]>a[0] else None,
    }


def ranges(rows):
    if not rows:
        return None
    shapes = Counter(len(v) for v in rows)
    if len(shapes) != 1:
        return {"length_counts": dict(shapes), "inconsistent_dimensions": True}
    a = np.asarray(rows, dtype=np.float64)
    if not a.shape[1]:
        return {"dimension": 0, "count": len(a), "nonfinite_values": 0}
    clean = np.where(np.isfinite(a), a, np.nan)
    with np.errstate(all="ignore"):
        return {"dimension": a.shape[1], "count": len(a),
                "nonfinite_values": int(np.sum(~np.isfinite(a))),
                "min": np.nanmin(clean, axis=0).tolist(), "max": np.nanmax(clean, axis=0).tolist(),
                "range": (np.nanmax(clean, axis=0)-np.nanmin(clean, axis=0)).tolist(),
                "first": a[0].tolist(), "last": a[-1].tolist()}


def scalar_stats(values):
    a = np.asarray(values, dtype=float)
    if not len(a):
        return None
    return {"min": float(np.min(a)), "median": float(np.median(a)),
            "p95": float(np.quantile(a,.95)), "max": float(np.max(a))}


def audit_one(args):
    filename, root = args
    p, root = Path(filename), Path(root)
    started = time.monotonic()
    out = {"path": str(p), "relative_path": str(p.relative_to(root)),
           "episode": str(p.parent.relative_to(root)), "bytes": p.stat().st_size, "flags": []}
    try:
        with p.open("rb") as f:
            reader = make_reader(f, decoder_factories=[DecoderFactory()], validate_crcs=True)
            summary = reader.get_summary()
            if summary is None or summary.statistics is None:
                raise ValueError("Missing MCAP summary/statistics")
            stat = summary.statistics
            channels = summary.channels
            topics = sorted({c.topic for c in channels.values()})
            noncamera = [t for t in topics if not t.startswith('/camera/')]
            summary_counts = Counter()
            for cid, count in stat.channel_message_counts.items():
                summary_counts[channels[cid].topic] += count
            out.update({"topics": topics, "summary_message_count": stat.message_count,
                        "summary_topic_counts": dict(summary_counts),
                        "start_ns": stat.message_start_time, "end_ns": stat.message_end_time,
                        "duration_s": (stat.message_end_time-stat.message_start_time)/1e9,
                        "chunk_count": len(summary.chunk_indexes)})
            # Index timestamps are sorted by physical record offset, not by timestamp.
            index_times = defaultdict(list)
            read_chunks = crc_chunks = 0
            for chunk in sorted(summary.chunk_indexes, key=lambda c: c.chunk_start_offset):
                if any(channels[cid].topic in noncamera for cid in chunk.message_index_offsets):
                    read_chunks += 1
                    f.seek(chunk.chunk_start_offset + 9 + 24)
                    crc_chunks += bool(struct.unpack('<I', f.read(4))[0])
                for cid, offset in chunk.message_index_offsets.items():
                    f.seek(offset)
                    opcode, length = struct.unpack('<BQ', f.read(9))
                    if opcode != 7:
                        raise ValueError(f"Expected message index at {offset}, got {opcode}")
                    index = MessageIndex.read(ReadDataStream(f))
                    if index.channel_id != cid:
                        raise ValueError("Message index channel disagrees with summary")
                    index_times[channels[cid].topic].extend(ts for ts, _ in sorted(index.records, key=lambda a:a[1]))
            out['crc_coverage'] = {"noncamera_related_chunks_read": read_chunks,
                                   "those_with_nonzero_crc": crc_chunks,
                                   "validate_crcs_enabled": True,
                                   "camera_payload_crc_complete": False}
            indexed_counts = {t: len(index_times[t]) for t in topics}
            out['indexed_topic_counts'] = indexed_counts
            out['index_summary_agree'] = indexed_counts == dict(summary_counts)
            if not out['index_summary_agree']:
                out['flags'].append('message_index_summary_count_mismatch')
            overlap_topics = [t for t in topics if t.startswith('/camera/')] + ['/hal/joint_states']
            bounds = {t: {'first_ns':min(index_times[t]), 'last_ns':max(index_times[t])} for t in overlap_topics if index_times[t]}
            common_start=max(v['first_ns'] for v in bounds.values()) if bounds else None
            common_end=min(v['last_ns'] for v in bounds.values()) if bounds else None
            out['camera_hal_log_overlap'] = {
                'per_topic_bounds':bounds, 'common_start_ns':common_start, 'common_end_ns':common_end,
                'common_duration_s':max(0,(common_end-common_start)/1e9) if bounds else 0,
                'start_spread_s':(max(v['first_ns'] for v in bounds.values())-min(v['first_ns'] for v in bounds.values()))/1e9 if bounds else None,
                'end_spread_s':(max(v['last_ns'] for v in bounds.values())-min(v['last_ns'] for v in bounds.values()))/1e9 if bounds else None,
                'caveat':'Temporal bounds only; interior gaps require per-topic log timing checks; camera header clocks not used.'}

            metadata = yaml.safe_load((p.parent/'metadata.yaml').read_text())['rosbag2_bagfile_information']
            yaml_counts = {x['topic_metadata']['name']: x['message_count'] for x in metadata['topics_with_message_count']}
            comparisons = {
                "message_count": metadata['message_count'] == stat.message_count,
                "topic_counts": yaml_counts == dict(summary_counts),
                "start_time": metadata['starting_time']['nanoseconds_since_epoch'] == stat.message_start_time,
                "duration": metadata['duration']['nanoseconds'] == stat.message_end_time-stat.message_start_time,
            }
            out['yaml_summary_comparison'] = comparisons
            if not all(comparisons.values()):
                out['flags'].append('yaml_summary_mismatch')

            logs, pubs, headers = defaultdict(list), defaultdict(list), defaultdict(list)
            frames = defaultdict(Counter)
            vectors = defaultdict(lambda: defaultdict(list))
            names = defaultdict(Counter)
            malformed = Counter()
            move_pos, move_quat, move_speed, move_flags = [], [], [], defaultdict(Counter)
            for entry in reader.iter_decoded_messages(topics=noncamera, log_time_order=False):
                t, raw, m = entry.channel.topic, entry.message, entry.decoded_message
                logs[t].append(raw.log_time); pubs[t].append(raw.publish_time)
                if hasattr(m, 'header'):
                    headers[t].append(m.header.stamp.sec*10**9+m.header.stamp.nanosec)
                    frames[t][m.header.frame_id] += 1
                if hasattr(m,'name'):
                    names[t][tuple(m.name)] += 1
                    if len(m.name) != len(m.position): malformed[t] += 1
                    for key in ('position','velocity','effort'):
                        values = list(getattr(m,key))
                        vectors[t][key].append(values)
                        if key != 'position' and len(values) not in (0,len(m.name)):
                            malformed[t] += 1
                elif hasattr(m,'position'):
                    vectors[t]['position'].append([float(m.position)])
                    vectors[t]['max_effort'].append([float(m.max_effort)])
                if t == '/move/State':
                    pos, quat, speed = m.odometry.position, m.odometry.orientation, m.speed
                    move_pos.append([pos.x,pos.y,pos.z])
                    move_quat.append([quat.x,quat.y,quat.z,quat.w])
                    move_speed.append([speed.linear.x,speed.linear.y,speed.linear.z,
                                       speed.angular.x,speed.angular.y,speed.angular.z])
                    for key in ('is_soft_auto','is_hard_auto','is_joystick_on','is_handle_on','is_parking_mode','is_avoid'):
                        move_flags[key][str(getattr(m,key))] += 1
            out['noncamera_decoded_count'] = sum(len(v) for v in logs.values())
            out['decoded_summary_agree'] = all(len(logs[t]) == summary_counts[t] for t in noncamera)
            if not out['decoded_summary_agree']:
                out['flags'].append('decoded_summary_count_mismatch')
            out['timing'] = {}
            for t in topics:
                td = {"log": timing(logs[t] if t in noncamera else index_times[t]),
                      "source": "decoded_CDR" if t in noncamera else "message_index_only"}
                if t in noncamera:
                    td['publish'] = timing(pubs[t]); td['header'] = timing(headers[t])
                    td['frame_ids'] = dict(frames[t])
                    td['publish_log_equal_count'] = int(np.sum(np.asarray(pubs[t]) == np.asarray(logs[t])))
                    if headers[t]:
                        td['log_minus_header_s'] = scalar_stats((np.asarray(logs[t],dtype=np.int64)-np.asarray(headers[t],dtype=np.int64))/1e9)
                out['timing'][t] = td
                for clock in ('log','publish','header'):
                    c = td.get(clock,{})
                    if c.get('backward_steps'):out['flags'].append('timestamp_backward:'+t+':'+clock)
                    if c.get('duplicate_steps'):out['flags'].append('timestamp_duplicates:'+t+':'+clock)
                    if c.get('large_gap_count'):out['flags'].append('timestamp_large_gap:'+t+':'+clock)

            out['joints'] = {}
            for t, cols in vectors.items():
                j = {k:ranges(v) for k,v in cols.items()}
                j['name_variants'] = [{"names":list(k),"count":v} for k,v in names[t].items()]
                j['malformed_array_messages'] = malformed[t]
                out['joints'][t] = j
                if malformed[t] or len(names[t]) > 1:
                    out['flags'].append('joint_layout_inconsistent:'+t)
                if any(v and v.get('nonfinite_values',0) for v in j.values() if isinstance(v,dict)):
                    out['flags'].append('nonfinite_joint_values:'+t)
            hal = out['joints'].get('/hal/joint_states',{})
            if len(hal.get('name_variants',[])) == 1 and hal.get('position',{}).get('range'):
                jnames, jranges = hal['name_variants'][0]['names'], hal['position']['range']
                groups = {"left_arm":'arm_l_', "right_arm":'arm_r_', "body":'body_', "head":'head_', "hands":'hand_'}
                out['hal_group_motion'] = {group:{name:span for name,span in zip(jnames,jranges) if name.startswith(prefix)} for group,prefix in groups.items()}
                out['hal_group_max_range'] = {k:max(v.values(),default=0) for k,v in out['hal_group_motion'].items()}
            if move_pos:
                pos, quat, speed = map(lambda v:np.asarray(v,dtype=float),(move_pos,move_quat,move_speed))
                qnorm = np.linalg.norm(quat,axis=1)
                ts = np.asarray(headers['/move/State'],dtype=np.int64)
                dt = np.diff(ts)/1e9
                good = dt>0
                derivative = np.diff(pos,axis=0)[good]/dt[good,None]
                speed_mid = (speed[:-1,:3]+speed[1:,:3])[good]/2
                diff = derivative-speed_mid
                stationary = np.linalg.norm(derivative,axis=1)<1e-9
                inconsistent = stationary & (np.linalg.norm(speed_mid,axis=1)>1e-6)
                ms = {
                    "count":len(pos), "position":ranges(move_pos), "quaternion":ranges(move_quat),
                    "speed_xyz_wxyz":ranges(move_speed), "boolean_fields":{k:dict(v) for k,v in move_flags.items()},
                    "nonfinite_values":int(np.sum(~np.isfinite(pos))+np.sum(~np.isfinite(quat))+np.sum(~np.isfinite(speed))),
                    "quaternion_zero_count":int(np.sum(qnorm==0)),
                    "quaternion_norm_outside_1pm0_001_count":int(np.sum(abs(qnorm-1)>0.001)),
                    "peak_abs_linear_xyz":np.max(np.abs(speed[:,:3]),axis=0).tolist(),
                    "peak_abs_angular_xyz":np.max(np.abs(speed[:,3:]),axis=0).tolist(),
                    "peak_linear_norm":float(np.max(np.linalg.norm(speed[:,:3],axis=1))),
                    "speed_nonzero_count":int(np.sum(np.linalg.norm(speed,axis=1)>1e-6)),
                    "position_derivative_vs_speed_midpoint_rmse_xyz":np.sqrt(np.mean(diff**2,axis=0)).tolist() if len(diff) else None,
                    "derivative_comparison_intervals":int(np.sum(good)),
                    "stationary_position_nonzero_speed_intervals":int(np.sum(inconsistent)),
                    "stationary_position_nonzero_speed_duration_s":float(np.sum(dt[good][inconsistent])),
                    "derivative_diagnostic_caveat":"Numerical check only: no verified pose/twist frame or unit contract; not calibrated physical velocity error.",
                }
                out['move'] = ms
                if ms['quaternion_norm_outside_1pm0_001_count']:out['flags'].append('move_invalid_quaternion')
                if ms['stationary_position_nonzero_speed_intervals']:out['flags'].append('move_stationary_pose_with_nonzero_speed')
                if ms['nonfinite_values']:out['flags'].append('move_nonfinite_values')
                out['hal_move_time_comparison'] = {}
                for kind, source in [('log',logs),('publish',pubs),('header',headers)]:
                    a,b = np.asarray(source['/hal/joint_states'],dtype=np.int64),np.asarray(source['/move/State'],dtype=np.int64)
                    eq = len(a)==len(b)
                    out['hal_move_time_comparison'][kind] = {"equal_count":bool(eq), "all_equal":bool(eq and np.array_equal(a,b)),
                        "same_index_diff_s":scalar_stats((b-a)/1e9) if eq else None}
            out['audit_success'] = True
            out['flags'] = sorted(set(out['flags']))
    except Exception as exc:
        out['audit_success'] = False
        out['error'] = str(exc)
        out['traceback'] = traceback.format_exc()
        out['flags'].append('audit_error')
    out['elapsed_s'] = round(time.monotonic()-started,3)
    return out


def aggregate(rows, root, elapsed):
    ok = [r for r in rows if r.get('audit_success')]
    flags = defaultdict(list)
    for r in rows:
        for flag in r['flags']:flags[flag].append(r['relative_path'])
    topic_totals = Counter()
    layouts = defaultdict(Counter)
    joint_global = defaultdict(dict)
    for r in ok:
        topic_totals.update(r['summary_topic_counts'])
        for topic, j in r['joints'].items():
            for variant in j['name_variants']:layouts[topic][tuple(variant['names'])] += 1
            if len(j['name_variants'])==1 and j['position'].get('min'):
                for name,lo,hi in zip(j['name_variants'][0]['names'],j['position']['min'],j['position']['max']):
                    previous=joint_global[topic].setdefault(name,{'min':lo,'max':hi})
                    previous['min']=min(previous['min'],lo);previous['max']=max(previous['max'],hi)
    moves = [r for r in ok if 'move' in r]
    motion = {k:scalar_stats([r['hal_group_max_range'][k] for r in ok if k in r.get('hal_group_max_range',{})]) for k in ['left_arm','right_arm','body','head','hands']}
    timing_overview = {}
    for topic in topic_totals:
        timed = [r for r in ok if topic in r['timing']]
        clocks = {}
        for kind in ('log','publish','header'):
            checked = [r for r in timed if r['timing'][topic].get(kind,{}).get('count')]
            if not checked:continue
            timing_rows=[r['timing'][topic][kind] for r in checked]
            max_gap_row=max(checked,key=lambda r:r['timing'][topic][kind]['max_gap_s'] or 0)
            clocks[kind] = {
                'checked_episodes':len(checked),'checked_messages':sum(t['count'] for t in timing_rows),
                'backward_steps':sum(t['backward_steps'] for t in timing_rows),
                'duplicate_steps':sum(t['duplicate_steps'] for t in timing_rows),
                'large_gap_count':sum(t['large_gap_count'] for t in timing_rows),
                'max_gap_s':max_gap_row['timing'][topic][kind]['max_gap_s'],
                'max_gap_episode':max_gap_row['relative_path'],
                'episode_max_gap_distribution_s':scalar_stats([t['max_gap_s'] for t in timing_rows if t['max_gap_s'] is not None]),
            }
        timing_overview[topic]=clocks
    summary = {
        'created_utc':datetime.now(timezone.utc).isoformat(), 'source_root':str(root),
        'episode_count':len(rows),'success_count':len(ok),'error_count':len(rows)-len(ok),
        'elapsed_s':elapsed,'total_bytes':sum(r['bytes'] for r in rows),
        'total_summary_messages':sum(r['summary_message_count'] for r in ok),
        'total_noncamera_decoded_messages':sum(r['noncamera_decoded_count'] for r in ok),
        'total_duration_s':sum(r['duration_s'] for r in ok), 'topic_message_counts':dict(topic_totals),
        'yaml_summary_match_episodes':sum(all(r['yaml_summary_comparison'].values()) for r in ok),
        'index_summary_match_episodes':sum(r['index_summary_agree'] for r in ok),
        'decoded_summary_match_episodes':sum(r['decoded_summary_agree'] for r in ok),
        'flag_episode_counts':{k:len(v) for k,v in flags.items()},
        'joint_name_layouts':{t:[{'names':list(n),'episode_count':c} for n,c in cs.items()] for t,cs in layouts.items()},
        'joint_global_position_ranges':dict(joint_global),
        'timing_overview':timing_overview,
        'hal_group_max_range_distribution':motion,
        'camera_hal_log_overlap':{
            'start_spread_s_distribution':scalar_stats([r['camera_hal_log_overlap']['start_spread_s'] for r in ok]),
            'end_spread_s_distribution':scalar_stats([r['camera_hal_log_overlap']['end_spread_s'] for r in ok]),
            'common_duration_s_distribution':scalar_stats([r['camera_hal_log_overlap']['common_duration_s'] for r in ok]),
            'max_start_spread_episode':max(ok,key=lambda r:r['camera_hal_log_overlap']['start_spread_s'])['relative_path'] if ok else None,
            'max_end_spread_episode':max(ok,key=lambda r:r['camera_hal_log_overlap']['end_spread_s'])['relative_path'] if ok else None},
        'motion_review_hints':{'threshold_raw_units':5,'caveat':'Range screening only; units and legal limits are unverified.',
            **{k+'_range_gt5_episodes':sum(r.get('hal_group_max_range',{}).get(k,0)>5 for r in ok) for k in ['left_arm','right_arm','body','head','hands']}},
        'move':{
            'episode_count':len(moves),
            'message_count':sum(r['move']['count'] for r in moves),
            'invalid_quaternion_messages':sum(r['move']['quaternion_norm_outside_1pm0_001_count'] for r in moves),
            'zero_quaternion_messages':sum(r['move']['quaternion_zero_count'] for r in moves),
            'speed_nonzero_messages':sum(r['move']['speed_nonzero_count'] for r in moves),
            'stationary_position_nonzero_speed_intervals':sum(r['move']['stationary_position_nonzero_speed_intervals'] for r in moves),
            'peak_linear_norm_distribution':scalar_stats([r['move']['peak_linear_norm'] for r in moves]),
            'peak_linear_episode':max(moves,key=lambda r:r['move']['peak_linear_norm'])['relative_path'] if moves else None,
            'peak_abs_linear_xyz':np.max([r['move']['peak_abs_linear_xyz'] for r in moves],axis=0).tolist() if moves else None,
            'peak_abs_angular_xyz':np.max([r['move']['peak_abs_angular_xyz'] for r in moves],axis=0).tolist() if moves else None,
            'all_zero_angular_velocity_episodes':sum(max(r['move']['peak_abs_angular_xyz'])==0 for r in moves),
            'zero_start_and_end_position_episodes':sum(r['move']['position']['first']==[0,0,0] and r['move']['position']['last']==[0,0,0] for r in moves),
            'position_ranges_xyz_variants':dict(Counter(str(r['move']['position']['range']) for r in moves)),
            'hal_move_time_all_equal_episode_counts':{kind:sum(r['hal_move_time_comparison'][kind]['all_equal'] for r in moves) for kind in ['log','publish','header']},
        },
        'crc':{'noncamera_related_chunks_read':sum(r['crc_coverage']['noncamera_related_chunks_read'] for r in ok),
               'nonzero_crc_chunks_validated':sum(r['crc_coverage']['those_with_nonzero_crc'] for r in ok)},
        'coverage':{
            'noncamera':'Every noncamera message decoded; log/publish/header times, joint arrays, move numeric fields checked.',
            'camera':'Summary + all message indexes only. Image pixels, image codec integrity, camera header/publish time not checked.',
            'crc':'validate_crcs=True for read noncamera-related chunks where CRC is nonzero; not all-file CRC or sensor semantic certification.',
            'quality':'No success/failure annotation, coordinate calibration, control-unit contract, or robot limit certification.'},
    }
    return summary, dict(flags)


def dump(path, value):
    # Preserve a valid JSON artifact even if source contains NaN or infinity.
    def clean(v):
        if isinstance(v,float) and not np.isfinite(v):return None
        if isinstance(v,dict):return {str(k):clean(x) for k,x in v.items()}
        if isinstance(v,(list,tuple)):return [clean(x) for x in v]
        return v
    path.write_text(json.dumps(clean(value),ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw'))
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--limit',type=int,default=0)
    args=parser.parse_args()
    paths=sorted(args.root.glob('*/episode*/*.mcap'))
    if args.limit:paths=paths[:args.limit]
    if not paths:raise SystemExit('No MCAP files matched')
    args.out.mkdir(parents=True,exist_ok=True)
    start=time.monotonic();rows=[]
    with ProcessPoolExecutor(max_workers=args.workers) as pool, (args.out/'episodes.jsonl').open('w') as stream:
        for row in pool.map(audit_one,((str(p),str(args.root)) for p in paths),chunksize=1):
            rows.append(row)
            stream.write(json.dumps(row,ensure_ascii=False,allow_nan=True)+'\n');stream.flush()
            if len(rows)%20==0 or len(rows)==len(paths):
                progress={'completed':len(rows),'total':len(paths),'errors':sum(not r.get('audit_success') for r in rows),'elapsed_s':round(time.monotonic()-start,1),'last':row['relative_path']}
                print(json.dumps(progress),flush=True);dump(args.out/'progress.json',progress)
    summary,flags=aggregate(rows,args.root,time.monotonic()-start)
    dump(args.out/'summary.json',summary);dump(args.out/'flags.json',flags)
    fields=['episode','relative_path','bytes','audit_success','duration_s','summary_message_count','noncamera_decoded_count','left_arm_max_range','right_arm_max_range','body_max_range','peak_linear_speed','move_zero_quaternions','flags']
    with (args.out/'episodes.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for r in rows:
            v={k:r.get(k) for k in fields};g=r.get('hal_group_max_range',{})
            v.update(left_arm_max_range=g.get('left_arm'),right_arm_max_range=g.get('right_arm'),body_max_range=g.get('body'),peak_linear_speed=r.get('move',{}).get('peak_linear_norm'),move_zero_quaternions=r.get('move',{}).get('quaternion_zero_count'),flags=';'.join(r['flags']))
            writer.writerow(v)
    print(json.dumps({'complete':len(rows),'errors':summary['error_count'],'out':str(args.out),'elapsed_s':summary['elapsed_s']}),flush=True)


if __name__=='__main__':
    main()
