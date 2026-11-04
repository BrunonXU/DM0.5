"""Causal decision replay, with original actor actions and publication evidence.

Server replies are proposals. Only owner publications can establish their
consumed prefix. Human corrections use future approved targets relative to the
recorded observation, never smoothed commands or the actor's cancelled suffix.
"""
from __future__ import annotations

import bisect
import math

from .rlt_local_replay import HUMAN, PAUSED, RIGHT, _valid_observation, build_transitions


def prediction_key(row):
    return (row['model_id'], row['policy_epoch'], row['request_id'],
            row['observation_id'], row['observation_monotonic_ns'])


def _close(a, b):
    return len(a) == len(b) and all(math.isfinite(x) and math.isfinite(y)
                                   and abs(x-y) <= 1e-6 for x, y in zip(a, b))


def _prefix(row, chunk):
    """Prove the approved target is the recorded interpolation of this plan."""
    s = row['policy_sampling']
    lo, hi, alpha = s['lower_index'], s['upper_index'], s['alpha']
    if not (-1 <= lo <= hi < 16 and 0 <= alpha <= 1 and hi-lo <= 1):
        raise ValueError('Invalid chunk interpolation evidence')
    left = s['anchor'] if lo == -1 else chunk['actions'][lo]
    right = chunk['actions'][hi]
    expected = [(1-alpha)*a+alpha*b for a, b in zip(left, right)]
    # Progressive gripper publications can lag the approved target. Preserve
    # their publication proof separately; selected is the controller input.
    if not _close([row['selected'][j] for j in RIGHT], [expected[j] for j in RIGHT]):
        raise ValueError('Published owner selection differs from the recorded plan')
    fractions = [0.]*16
    for i in range(lo+1):
        fractions[i] = 1.
    if hi != lo:
        fractions[hi] = alpha
    return fractions


def _human_record(row, observations, controls, ct, *, gamma, fps, human=True):
    """Re-anchor a verified human interval; first target is one tick ahead."""
    obs, nxt = observations[row['observation_id']], observations[row['next_observation_id']]
    start, end = obs['available_monotonic_ns'], nxt['available_monotonic_ns']
    period = 1e9/fps
    if end-start > round(16*period):
        options=[o for o in observations.values() if end-round(16*period) <= o['available_monotonic_ns'] < end
                 and _valid_observation(o,o['available_monotonic_ns'])]
        if not options:
            raise ValueError('No human observation inside the actor horizon')
        obs=min(options,key=lambda o:o['available_monotonic_ns'])
        start=obs['available_monotonic_ns']
    allowed=HUMAN if human else {'policy','hold','rlt_boundary_hold'}
    selected = controls[max(0,bisect.bisect_right(ct,start)-1):bisect.bisect_right(ct,end)]
    if any(c['source'] not in allowed for c in selected):
        last_bad=max(i for i,c in enumerate(selected) if c['source'] not in allowed)
        if last_bad+1 == len(selected):
            raise ValueError('Human endpoint is outside an active human interval')
        floor=selected[last_bad+1]['monotonic_ns']
        options=[o for o in observations.values() if floor <= o['available_monotonic_ns'] < end
                 and _valid_observation(o,o['available_monotonic_ns'])]
        if not options:
            raise ValueError('No causal human observation after the control boundary')
        obs=min(options,key=lambda o:o['available_monotonic_ns']); start=obs['available_monotonic_ns']
        selected=controls[bisect.bisect_right(ct,start)-1:bisect.bisect_right(ct,end)]
    count = min(16, math.ceil((end-start)/period))
    if count < 1 or end-start > round(16*period):
        raise ValueError('Human interval has no forward observation inside the actor horizon')
    if not selected or any(c['source'] not in allowed for c in selected):
        raise ValueError('Human observation crosses a control-owner boundary')
    if len({(c['policy_epoch'], c.get('intervention_id')) for c in selected}) != 1:
        raise ValueError('Human observation crosses an intervention boundary')
    grid = [min(end,start+round((i+1)*period)) for i in range(count)]
    ticks = [controls[bisect.bisect_right(ct,t)-1] for t in grid]
    if any(t-c['monotonic_ns'] > 50_000_000 or c.get('selected') is None for t,c in zip(grid,ticks)):
        raise ValueError('Missing approved human target')
    r = dict(row, schema='f1_decision_transition_v1', decision_ns=start, next_decision_ns=end,
             observation_id=obs['observation_id'], state16=obs['state16'],
             elapsed_steps=(end-start)/period, discount=gamma**((end-start)/period),
             selected16=[c['selected'] for c in ticks], submitted16=[c['submitted'] for c in ticks],
             command_sequences=[c['command_sequence'] for c in ticks], action_grid_ns=grid,
             action_time_mask=[True]*count+[False]*(16-count),
             intervene_flags=[human]*count+[False]*(16-count),
             execution_fraction=[1.]*(count-1)+[min(1.,(end-start)/period-count+1)]+[0.]*(16-count),
             decision_kind='human_future_targets' if human else 'legacy_terminal_future_targets',
             original_decision_ns=row['decision_ns'])
    if 'action_control_sequences' in row:
        r['original_action_control_sequences'] = row['action_control_sequences']
        r['action_control_sequences'] = [c['stream_sequence'] for c in ticks]
    return r


def _actor_context(plan, adopted, observations, ordered, controls, ct, result):
    """Choose decision/endpoint and consumed publications without model access."""
    if plan.get('rtc') is not None:
        return _rtc_actor_context(plan, adopted, observations, ordered, controls, result)
    obs = observations[plan['observation_id']]
    terminal = result['terminal_observation']['available_monotonic_ns']
    begin = obs['available_monotonic_ns']
    if not _valid_observation(obs,begin) or obs['capture_started_monotonic_ns'] < result['started_ns']:
        raise ValueError('Invalid decision observation')
    owned = [c for c in controls if c['source']=='policy'
             and c.get('policy_request_id')==plan['request_id']
             and c['policy_epoch']==plan['policy_epoch']
             and begin <= c['monotonic_ns'] <= terminal]
    if not owned:
        raise ValueError('Adopted proposal has no actual publication')
    # Prefer the next real decision, provided only passive holds separate
    # it from this action. Otherwise stop before another owner acts.
    last = owned[-1]['monotonic_ns']
    later = controls[bisect.bisect_right(ct,last):bisect.bisect_right(ct,terminal)]
    boundary = next((c['monotonic_ns'] for c in later
                     if c['source'] in HUMAN | (PAUSED-{'hold'}) or c['source']=='policy'), terminal)
    nxt = None
    following = [p for p in adopted if p['adopted_monotonic_ns'] > plan['adopted_monotonic_ns']]
    if following:
        candidate=observations[following[0]['observation_id']]
        between=controls[bisect.bisect_right(ct,last):bisect.bisect_right(ct,candidate['available_monotonic_ns'])]
        if (candidate['capture_started_monotonic_ns'] >= last
                and not any(c['source'] in HUMAN or c['source'] in PAUSED-{'hold'} for c in between)):
            nxt=candidate
    if nxt is None:
        candidates=[o for o in ordered if begin < o['available_monotonic_ns'] <= boundary
                    and _valid_observation(o,o['available_monotonic_ns'])]
        after=[o for o in candidates if o['capture_started_monotonic_ns'] >= last]
        nxt=after[0] if after else (candidates[-1] if candidates else None)
    if nxt is None:
        raise ValueError('No causal observation before next controller takes over')
    end=nxt['available_monotonic_ns']
    # A transition cannot use a command issued after its endpoint was
    # captured. This can shorten a cancelled chunk by another fraction.
    used=[c for c in owned if c['monotonic_ns'] <= nxt['capture_started_monotonic_ns']]
    if not used:
        raise ValueError('Endpoint predates the first executed actor action')
    return obs, nxt, used


def _rtc_actor_context(plan, adopted, observations, ordered, controls, result):
    """An RTC decision includes the already committed old-plan prefix.

    Stop at the next decision observation, before that decision can alter the
    continuation. Every selected target is proved against its original owner
    and against this effective chunk separately by the caller.
    """
    obs = observations[plan['observation_id']]
    start = obs['capture_started_monotonic_ns']
    terminal = result['terminal_observation']['available_monotonic_ns']
    if not _valid_observation(obs, obs['available_monotonic_ns']):
        raise ValueError('Invalid RTC decision observation')
    following = [observations[p['observation_id']] for p in adopted
                 if p['adopted_monotonic_ns'] > plan['adopted_monotonic_ns']]
    following = [o for o in following if o['capture_started_monotonic_ns'] > start]
    cutoff = min((o['capture_started_monotonic_ns'] for o in following), default=terminal)
    relevant = [c for c in controls if start <= c['monotonic_ns'] <= cutoff]
    boundary = next((c['monotonic_ns'] for c in relevant
                     if c['source'] not in {'policy','hold'} or c['policy_epoch'] != plan['policy_epoch']), cutoff)
    if following and cutoff == boundary:
        nxt = min(following, key=lambda o:o['capture_started_monotonic_ns'])
    else:
        options = [o for o in ordered if start < o['capture_started_monotonic_ns'] <= boundary
                   and o['available_monotonic_ns'] <= terminal and _valid_observation(o,o['available_monotonic_ns'])]
        if not options:
            raise ValueError('No causal RTC endpoint before authority changes')
        nxt = options[-1]
    used = [c for c in relevant if c['source']=='policy'
            and c['monotonic_ns'] <= nxt['capture_started_monotonic_ns']]
    if not used or not any(c.get('policy_request_id') == plan['request_id'] for c in used):
        raise ValueError('RTC candidate has no actual publication before next decision')
    if any(c['source'] != 'policy' for c in relevant
           if used[0]['monotonic_ns'] < c['monotonic_ns'] < nxt['capture_started_monotonic_ns']):
        raise ValueError('RTC decision crosses an unmodelled hold')
    return obs, nxt, used


def _rtc_prefix(control, chunk, context, original_chunks):
    own = original_chunks.get((control['policy_epoch'],control.get('policy_request_id')))
    if own is None:
        raise ValueError('RTC original owner plan is missing')
    _prefix(control, own)
    origin = context.get('plan_origin_ns', 0)
    if context['bootstrap']:
        origin = control['policy_sampling']['chunk_started_monotonic_ns']
        anchor = control['policy_sampling'].get('anchor')
        if anchor is None:
            # Initial anchor is captured by the first actual sample and saved
            # by the caller; never substitute feedback or a later command.
            anchor = context.get('bootstrap_anchor')
    else:
        base = original_chunks.get((control['policy_epoch'],context['base_plan_id']))
        if base is None:
            raise ValueError('RTC committed base plan is missing')
        anchor = base['actions'][context['shift_steps']-1]
    sampling = control['policy_sampling']
    owner_u = sampling['lower_index']+1+sampling['alpha']
    # Use the exact sampler coordinates, not the later publication timestamp.
    shift_float = (sampling['chunk_started_monotonic_ns']-origin)*30/1e9
    shift = round(shift_float)
    if abs(shift_float-shift) > 1e-6:
        raise ValueError('RTC owner origin is off the committed waypoint grid')
    u = owner_u+shift
    if not 0 <= u <= 16+1e-6 or anchor is None:
        raise ValueError('RTC publication outside proven action horizon')
    lo = -1 if u < 1 else min(int(u-1),15)
    hi = 0 if u < 1 else min(lo+1,15)
    alpha = u if u < 1 else min(u-1,15)-lo
    row = dict(control, policy_sampling=dict(lower_index=lo, upper_index=hi, alpha=alpha, anchor=anchor))
    return _prefix(row,chunk)


def required_decision_observations(streams, result, legacy=None, *, gamma=.999, fps=30):
    """Sparse image membership shared by the packer and decision replay.

    Retain legacy anchors for compatibility, plus original actor queries and
    human anchors advanced past waits. No server/model dependency on F1.
    """
    legacy = build_transitions(streams, result, gamma=gamma, fps=fps) if legacy is None else legacy
    wanted = {r[k] for r in legacy for k in ('observation_id', 'next_observation_id')}
    observations = {o['observation_id']: o for o in streams['observations']}
    controls = streams['control']; ct = [c['monotonic_ns'] for c in controls]
    terminal = result['terminal_observation']['available_monotonic_ns']
    adopted = [e['plan'] for e in streams.get('events', []) if e.get('event')=='rlt_plan_adopted'
               and e['plan']['phase']=='actor' and result['started_ns'] <= e['monotonic_ns'] < terminal]
    for row in legacy:
        if any(row['intervene_flags']):
            try:
                r = _human_record(row, observations, controls, ct, gamma=gamma, fps=fps)
                wanted.update((r['observation_id'], r['next_observation_id']))
            except ValueError:
                pass
    for plan in adopted:
        try:
            obs, nxt, _ = _actor_context(plan, adopted, observations, streams['observations'], controls, ct, result)
            wanted.update((obs['observation_id'], nxt['observation_id']))
        except (KeyError, ValueError):
            pass
    return sorted(wanted)


def build_decision_transitions(streams, result, predictions, *, gamma=.999, fps=30):
    """Return verified records and explicit exclusions, without changing labels.

    Existing prefix validation still owns reward/publication/sensor integrity.
    Autonomous records are rebuilt from actual requests, not arbitrary windows.
    Missing server evidence is excluded explicitly, never filled by FK guesses.
    """
    legacy = build_transitions(streams,result,gamma=gamma,fps=fps)
    observations = {r['observation_id']:r for r in streams['observations']}
    ordered = streams['observations']
    controls = streams['control']; ct = [c['monotonic_ns'] for c in controls]
    terminal = result['terminal_observation']['available_monotonic_ns']
    rows, excluded = [], []
    for r in legacy:
        if not any(r['intervene_flags']):
            continue
        try:
            rows.append(_human_record(r, observations, controls, ct, gamma=gamma, fps=fps))
        except ValueError as exc:
            excluded.append(dict(kind='human', decision_ns=r['decision_ns'], reason=str(exc)))

    chunks = {prediction_key(c):c for c in streams.get('policy_chunks',[])}
    owner_chunks = {(c['policy_epoch'],c['request_id']):c for c in streams.get('policy_chunks',[])}
    adopted = [e['plan'] for e in streams.get('events',[]) if e['event']=='rlt_plan_adopted'
               and e['plan']['phase']=='actor' and result['started_ns'] <= e['monotonic_ns'] < terminal]
    for plan in adopted:
        key = prediction_key(plan)
        try:
            chunk, pred = chunks[key], predictions[key]
            obs, nxt, used = _actor_context(plan, adopted, observations, ordered, controls, ct, result)
            replay = pred['prediction_replay']
            if (not pred.get('success') or not _close(obs['state16'], replay['observation_anchor_state16'])
                    or pred['rlt']['actor_version'] != result['actor_version']
                    or pred['model_version'] != plan['model_version']
                    or pred['actions'] != chunk['actions']):
                raise ValueError('Server proposal identity differs from raw robot journal')
            begin, end = obs['available_monotonic_ns'], nxt['available_monotonic_ns']
            rtc = plan.get('rtc')
            if rtc is not None:
                if replay.get('rtc') != rtc or replay.get('prefix_contract') != 'known_prefix_detached_v1':
                    raise ValueError('RTC execution context differs from server proposal')
                rtc = dict(rtc)
                rtc['bootstrap_anchor'] = next((c['policy_sampling'].get('anchor') for c in used
                                               if c['policy_sampling'].get('anchor') is not None), None)
            fractions=[0.]*16
            for c in used:
                if c.get('publication_complete') is not True or c.get('stage')!='completed':
                    raise ValueError('Unpublished actor selection')
                f=_rtc_prefix(c,chunk,rtc,owner_chunks) if rtc is not None else _prefix(c,chunk)
                fractions=[max(a,b) for a,b in zip(fractions,f)]
            if rtc is not None and not rtc['bootstrap']:
                # The camera may arrive partway through the first committed
                # interpolation interval. That already-consumed fraction is
                # in s_t, not a new action applied between s_t and s_{t+1}.
                offset=(obs['capture_started_monotonic_ns']-rtc['plan_origin_ns'])*fps/1e9
                if not -1e-6 <= offset < 1+1e-6:
                    raise ValueError('RTC observation is not on its declared decision grid')
                fractions[0]=max(0.,fractions[0]-max(0.,offset))
            count=sum(f>0 for f in fractions)
            if not count or fractions[0]<=0 or any(f>0 for f in fractions[count:]):
                raise ValueError('No contiguous consumed actor prefix')
            raw=replay['actor_normalized_actions32']
            if len(raw)!=16 or any(len(a)!=32 or not all(math.isfinite(v) for v in a) for a in raw):
                raise ValueError('Invalid original normalized actor action')
            final=end==terminal
            if rtc is not None:
                begin, end = obs['capture_started_monotonic_ns'], nxt['capture_started_monotonic_ns']
            rows.append(dict(schema='f1_decision_transition_v1', objective=result['objective'],
                episode_id=result['episode_id'], segment_id=result['segment_id'],
                decision_kind='actor_request', request_id=plan['request_id'],
                observation_id=obs['observation_id'], next_observation_id=nxt['observation_id'],
                state16=obs['state16'], decision_ns=begin, next_decision_ns=end,
                elapsed_steps=(end-begin)*fps/1e9, discount=gamma**((end-begin)*fps/1e9),
                action_time_mask=[True]*count+[False]*(16-count), execution_fraction=fractions,
                intervene_flags=[False]*16, normalized_actions10=[a[:10] for a in raw[:count]],
                command_sequences=[c['command_sequence'] for c in used],
                execution_started_ns=used[0]['monotonic_ns'], execution_last_ns=used[-1]['monotonic_ns'],
                endpoint_capture_ns=nxt['capture_started_monotonic_ns'],
                reward=result['reward'] if final else 0., terminated=final and result['terminated'],
                reward_known=True, terminal_known=True, next_state_verified=True,
                truncated=(final and not result['terminated']) or (not final and (count<16 or fractions[-1]<1)),
                boundary_reason='local_terminal' if final else 'next_observation',
                actor_version=result['actor_version'], assisted=result['assisted'],
                observation_run=0, server_prediction_sha256=pred.get('_source_line_sha256'),
                **({'rtc':plan['rtc'],'prefix_contract':'known_prefix_detached_v1'} if rtc is not None else {})))
        except (KeyError,ValueError) as exc:
            excluded.append(dict(kind='actor', request_id=plan['request_id'], reason=str(exc)))
    rows.sort(key=lambda r:r['decision_ns'])
    # Never silently drop the only real reward while claiming a complete task.
    if not rows or sum(r['terminated'] for r in rows)!=int(result['terminated']):
        raise ValueError('Decision reconstruction did not retain exactly one true terminal')
    return rows, dict(excluded=excluded, original_windows=len(legacy), decisions=len(adopted),
                     retained_actor=sum(r['decision_kind']=='actor_request' for r in rows),
                     retained_human=sum(r['decision_kind']=='human_future_targets' for r in rows))
