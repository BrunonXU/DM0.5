"""Offline feedback comparison on the original time axis; never time-warp traces."""
from bisect import bisect_left
import math
from .replay_data import NAMES


def crossings(series):
    """Observed crossings of 50% travel, not inferred actuator start times."""
    return [(t, 'close' if v >= 50 else 'open') for (p, old), (t, v) in zip(series, series[1:])
            if (old >= 50) != (v >= 50)]


def arm_summary(channels):
    """Equal joint weights: more receipts must not give a joint more influence."""
    valid = [c for c in channels if c['metrics'] and c['matched'] >= 2]
    metrics = None
    if valid:
        metrics = dict(mean_abs=sum(c['metrics']['mean_abs'] for c in valid)/len(valid),
                       rms=math.sqrt(sum(c['metrics']['rms']**2 for c in valid)/len(valid)),
                       max_abs=max(c['metrics']['max_abs'] for c in valid))
    return dict(unit='deg', joints_compared=len(valid), joints_total=len(channels),
                complete=len(valid) == len(channels), metrics=metrics,
                weighting='equal_joint_weight; minimum_two_matched_samples_per_joint')


def compare(segment, samples, *, max_gap_s, execution_status):
    channels = []
    source = segment.recording.state_series
    for j, name in enumerate(NAMES):
        original = [((t-segment.begin)/1e9, v) for t, v in (source[j] if source else ())]
        stamps = [t for t, _ in original]
        current = []
        for sample in samples:
            if not segment.recording.valid_mask[j]:
                continue
            t = sample['feedback_times_s'][j]
            if 0 <= t <= segment.duration and (not current or t > current[-1][0]):
                current.append((t, sample['actual'][j]))
        paired, missing = [], 0
        for t, actual in current:
            i = bisect_left(stamps, t)
            if i < len(stamps) and abs(stamps[i]-t) < 1e-8:
                expected = original[i][1]
            elif 0 < i < len(stamps) and stamps[i]-stamps[i-1] <= max_gap_s:
                left, right = original[i-1], original[i]
                expected = left[1]+(right[1]-left[1])*(t-left[0])/(right[0]-left[0])
            else:
                missing += 1; paired.append([t, None, actual]); continue
            paired.append([t, expected, actual])
        matched = [(t, actual-expected) for t, expected, actual in paired if expected is not None]
        metric = None
        if matched:
            worst = max(matched, key=lambda x: abs(x[1]))
            metric = dict(mean_abs=sum(abs(e) for _, e in matched)/len(matched),
                          rms=math.sqrt(sum(e*e for _, e in matched)/len(matched)),
                          max_abs=abs(worst[1]), max_at_s=worst[0],
                          compared_span_s=matched[-1][0]-matched[0][0])
        row = dict(name=name, unit='deg' if j < 14 else '%', matched=len(matched),
                   hardware_present=segment.recording.valid_mask[j],
                   missing=missing, original_samples=sum(0 <= t <= segment.duration for t in stamps),
                   replay_samples=len(current), metrics=metric,
                   # Downsample the plot only; metrics use every captured sample.
                   points=paired[::max(1, math.ceil(len(paired)/1500))])
        if j >= 14:
            a = crossings([(t,v) for t,v in original if 0 <= t <= segment.duration])
            b = crossings(current)
            same = segment.recording.valid_mask[j] and [d for _,d in a] == [d for _,d in b]
            row['crossings_50_percent'] = dict(original_count=len(a), replay_count=len(b),
                    comparable=same, deltas_s=[y[0]-x[0] for x,y in zip(a,b)] if same else None)
        channels.append(row)
    return dict(available=any(c['matched'] >= 2 for c in channels), execution_status=execution_status,
                basis='per_channel_host_receipt_time_relative_to_first_command; linear_interpolation; no_time_warp',
                sampling='feedback captured up to 50Hz; repeated receipts excluded',
                max_interpolation_gap_s=max_gap_s, channels=channels,
                arms_summary={key:arm_summary(rows) for key, rows in (
                    ('both', channels[:14]), ('left', channels[:7]), ('right', channels[7:14]))},
                verdict='偏差统计，不自动判定硬件合格；中断记录仅代表已执行部分')
