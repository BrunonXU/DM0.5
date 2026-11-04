"""Verified failure-terminal anchors and held-out critic checks, without reset data."""
from pathlib import Path
import json
import torch

from .rlt_online import sha, atomic_json


def subset(data, indices):
    return {k: {n: t[indices] for n, t in v.items()} if isinstance(v, dict) else v[indices]
            for k, v in data.items()}


def concatenate(values):
    from .rlt_supervision import expert_mask
    if any('action_time_mask' in v for v in values):
        values = [dict(v, expert_approved=expert_mask(v)) for v in values]
    # Historical synchronous replay has no committed context. Upgrade only
    # in memory when mixing it with explicitly versioned RTC observations.
    if any('committed_mask' in v.get('obs', {}) for v in values):
        from .rlt_prefix import add_empty_prefix
        values = [dict(v, obs=add_empty_prefix(v['obs']), next_obs=add_empty_prefix(v['next_obs'])) for v in values]
    return {k: {n: torch.cat([v[k][n] for v in values]) for n in values[0][k]}
            if isinstance(values[0][k], dict) else torch.cat([v[k] for v in values])
            for k in values[0]}


def migrate_failure_terminals(source, destination, feature_identity, action_stats):
    """Only previously reviewed FAILED placement endpoints retain their label.

    Old successes/retreat windows are deliberately not relabelled. These anchors
    train the critic only, never BC, and preserve the old episode partition.
    """
    source, destination = Path(source), Path(destination)
    manifest = json.loads((source/'manifest.json').read_text())
    if (manifest['schema'] != 'f1_rlt_dagger_replay_v1'
            or manifest['parent_bundle_sha256'] != feature_identity['rlt']['base_model_version']
            or manifest['encoder_manifest_sha256'] != feature_identity['rlt']['encoder_manifest_sha256']
            or manifest['action_norm_stats'] != action_stats):
        raise ValueError('Legacy terminal feature/action identity mismatch')
    for name, digest in manifest['files_sha256'].items():
        if Path(name).name != name or sha(source/name) != digest:
            raise ValueError('Legacy source checksum mismatch')
    old = torch.load(source/'replay.pt', map_location='cpu', weights_only=True)
    rows = [json.loads(line) for line in (source/'transitions.jsonl').read_text().splitlines()]
    selected = {'train':[], 'validation':[]}
    for i, row in enumerate(rows):
        if row.get('segment_outcome') != 'failure' or not row.get('terminated'):
            continue
        if (row.get('truncated') or row.get('actual_publications_verified') is not True
                or row.get('data_source') != 'autonomous_B1250'
                or row.get('action_space') != 'normalized_relative_eef10_from_submitted_joints_v1'
                or not bool(old['terminated'][i]) or bool(old['truncated'][i])
                or old['rewards'][i].count_nonzero() or any(row['rewards'])
                or len(row['action_grid_ns']) != 16
                or len(row['submitted16']) != 16
                or not all(a < b for a,b in zip(row['action_grid_ns'], row['action_grid_ns'][1:]))
                or not 15.99 < (row['next_decision_ns']-row['decision_ns'])*30/1e9 < 16.01):
            raise ValueError('Unverified legacy terminal cannot be migrated')
        selected[row['split']].append(i)
    groups = {split:{rows[i]['source_run'] for i in indices} for split,indices in selected.items()}
    if not all(groups.values()) or groups['train'] & groups['validation']:
        raise ValueError('Failure anchors require disjoint source episodes')
    destination.mkdir(parents=True, exist_ok=False)
    provenance = {}
    for split, indices in selected.items():
        current, nxt = old['current'][indices], old['next'][indices]
        n = len(indices)
        data = dict(obs={k:old[k][current] for k in ('z_rl','proprio','ref_chunk')},
                    next_obs={k:old[k][nxt] for k in ('z_rl','proprio','ref_chunk')},
                    actions=old['actions'][indices], action_time_mask=torch.ones(n,16,dtype=torch.bool),
                    intervene_flags=torch.zeros(n,16,dtype=torch.bool),
                    terminated=torch.ones(n,dtype=torch.bool), truncated=torch.zeros(n,dtype=torch.bool),
                    reward=torch.zeros(n), discount=old['discount'][indices],
                    elapsed_steps=torch.tensor([(rows[i]['next_decision_ns']-rows[i]['decision_ns'])*30/1e9 for i in indices]))
        torch.save(data, destination/(split+'.pt'))
        provenance[split] = [dict(index=i, episode=rows[i]['source_run'], segment=rows[i]['segment_id'],
                                 decision_ns=rows[i]['decision_ns'], next_decision_ns=rows[i]['next_decision_ns'])
                             for i in indices]
    atomic_json(destination/'manifest.json', dict(schema='f1_rlt_critic_terminals_v1',
        objective='approach_place_release_v1', feature_identity=feature_identity,
        source_manifest_sha256=sha(source/'manifest.json'), source=str(source),
        provenance=provenance, scope='critic_only_reviewed_failures_not_actor_replay',
        validation_scope='previous_development_holdout_not_new_blind_test',
        files_sha256={split+'.pt':sha(destination/(split+'.pt')) for split in selected}))


def load_terminals(root, feature_identity):
    root = Path(root); manifest = json.loads((root/'manifest.json').read_text())
    if (manifest.get('schema') != 'f1_rlt_critic_terminals_v1'
            or manifest.get('feature_identity') != feature_identity
            or manifest.get('objective') != 'approach_place_release_v1'):
        raise ValueError('Critic terminal contract mismatch')
    groups = [{row['episode'] for row in manifest['provenance'][s]} for s in ('train','validation')]
    if groups[0] & groups[1]: raise ValueError('Critic validation leakage')
    data = {}
    for split in ('train','validation'):
        if sha(root/(split+'.pt')) != manifest['files_sha256'][split+'.pt']:
            raise ValueError('Critic terminal cache changed')
        data[split] = torch.load(root/(split+'.pt'), weights_only=True, map_location='cpu')
        if (len(data[split]['actions']) != len(manifest['provenance'][split])
                or not data[split]['terminated'].all() or data[split]['truncated'].any()):
            raise ValueError('Nonterminal in critic anchor dataset')
    return data, manifest


def combine_training_suffixes(base_root, suffix_roots, destination, feature_identity):
    data, manifest = load_terminals(base_root, feature_identity)
    parts=[data['train']]; provenance=list(manifest['provenance']['train']); sources={}
    for root in map(Path,suffix_roots):
        meta=json.loads((root/'manifest.json').read_text())
        if (meta.get('schema')!='f1_local_replay_cache_v1'
                or meta.get('objective')!='approach_place_release_v1'
                or meta.get('learning_scope')!='critic_only_terminal_suffixes' or meta['split']!='train'
                or meta['feature_identity']!=feature_identity or meta.get('synthetic')):
            raise ValueError('Untrusted critic training suffix source')
        if meta['episode_id'] in {r['episode'] for r in manifest['provenance']['validation']}:
            raise ValueError('Critic validation leakage')
        for name,digest in meta['files_sha256'].items():
            if Path(name).name!=name or sha(root/name)!=digest:raise ValueError('Suffix cache changed')
        rows=[json.loads(line) for line in (root/'transitions.jsonl').read_text().splitlines()]
        part=torch.load(root/'replay.pt',weights_only=True)
        if (len(rows)!=len(part['actions']) or not part['terminated'].all()
                or part['truncated'].any()):
            raise ValueError('Expected only verified terminal suffixes')
        parts.append(part)
        provenance.extend(dict(episode=meta['episode_id'],segment=row['segment_id'],
                               decision_ns=row['decision_ns'],next_decision_ns=row['next_decision_ns']) for row in rows)
        sources[str(root)]=sha(root/'manifest.json')
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
    torch.save(concatenate(parts),destination/'train.pt')
    torch.save(data['validation'],destination/'validation.pt')
    atomic_json(destination/'manifest.json',dict(manifest,source=str(base_root),
        source_manifest_sha256=sha(Path(base_root)/'manifest.json'),suffix_sources=sources,
        provenance=dict(train=provenance,validation=manifest['provenance']['validation']),
        files_sha256={s+'.pt':sha(destination/(s+'.pt')) for s in ('train','validation')}))
    load_terminals(destination,feature_identity)


@torch.no_grad()
def critic_report(learner, data):
    if not len(data['actions']):
        return dict(metrics=dict(terminal_count=0,success_count=0,failure_count=0,finite=True),
                    checks=dict(both_outcomes=False),passed=False)
    q = learner.critic(learner.model._critic_state(data['obs']), data['actions'],
                       data['action_time_mask'], data['elapsed_steps'], data.get('execution_fraction'))
    terminal = data['terminated'].bool()
    success, failure = terminal & (data['reward']==1), terminal & (data['reward']==0)
    finite = bool(torch.isfinite(q).all())
    metrics = dict(terminal_count=int(terminal.sum()), success_count=int(success.sum()),
                   failure_count=int(failure.sum()), finite=finite,
                   q_min=float(q.min()), q_max=float(q.max()),
                   terminal_mae=float((q[terminal]-data['reward'][terminal,None]).abs().mean()) if terminal.any() else None,
                   success_min=float(q[success].min()) if success.any() else None,
                   failure_max=float(q[failure].max()) if failure.any() else None,
                   twin_disagreement=float((q[:,0]-q[:,1]).abs().max()))
    # Declared in advance relative to the 0/1 return range; a diagnostic gate,
    # not a confidence interval or evidence of improved robot success.
    checks = dict(finite=finite, both_outcomes=bool(success.any() and failure.any()),
                  success_calibrated=bool(success.any() and (q[success] >= .75).all()),
                  failure_calibrated=bool(failure.any() and (q[failure] <= .25).all()),
                  return_range=bool((q >= -.1).all() and (q <= 1.1).all()))
    return dict(metrics=metrics, checks=checks, passed=all(checks.values()))
