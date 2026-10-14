"""One continuity policy for new Pico and common capture sources."""
from .contract import CAMERAS, HUMAN, EXCLUDED, validate_row


def segments(rows, *, fps=30):
    if type(fps) is not int or not 1 <= fps <= 30: raise ValueError('fps must be integer 1..30')
    period = round(1e9/fps)
    groups, group = [], []
    rejected = 0
    previous = None
    def flush():
        nonlocal group
        if len(group) >= 2: groups.append(group)
        group = []
    for row in rows:
        validate_row(row)
        if previous is not None and row['time_ns'] <= previous['time_ns']:
            raise ValueError('reference time regressed')
        valid = (all(row['action_valid']) and row.get('control_source') in HUMAN
                 and row.get('phase') not in EXCLUDED and row.get('execution_mode') == 'powered'
                 and row['action_source'] in ('f1dagger_single_owner_publication', 'verified_pico_session_and_graph')
                 and type(row.get('state_max_age_ns')) is int and 0 <= row['state_max_age_ns'] <= 50_000_000
                 and type(row.get('arm_action_max_age_ns')) is int and 0 <= row['arm_action_max_age_ns'] <= 50_000_000
                 and all(0 <= row['time_ns']-f['received_ns'] <= 200_000_000 for f in row['images'].values()))
        discontinuity = False
        if previous is not None:
            discontinuity = (row['time_ns']-previous['time_ns'] > 50_000_000
                or any(row.get(k) != previous.get(k) for k in ('session_id','owner_epoch','intervention_id','phase'))
                or any(row['images'][k]['frame_id'] != previous['images'][k]['frame_id']+1 for k in CAMERAS)
                or row.get('boundary', False))
        previous = row
        if not valid:
            rejected += 1; flush(); continue
        if discontinuity: flush()
        group.append(row)
    flush()
    selected = []
    for group in groups:
        # Sampling never creates frames or repeats an input row. A missing slot
        # splits the output; source nanoseconds are retained in provenance.
        chunk, cursor, used = [], 0, -1
        for grid in range(group[0]['time_ns'], group[-1]['time_ns']+period//2, period):
            while cursor+1 < len(group) and abs(group[cursor+1]['time_ns']-grid) < abs(group[cursor]['time_ns']-grid): cursor += 1
            if cursor == used or abs(group[cursor]['time_ns']-grid) > period//2:
                if len(chunk) >= 2: selected.append(chunk)
                chunk = []; continue
            chunk.append(group[cursor]); used = cursor
        if len(chunk) >= 2: selected.append(chunk)
    return selected, dict(input_rows=len(rows), rejected_rows=rejected, segments=len(selected),
                          selected_rows=sum(map(len,selected)), fps=fps, fabricated_frames=0)
