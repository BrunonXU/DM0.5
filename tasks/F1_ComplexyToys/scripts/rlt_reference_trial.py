#!/usr/bin/env python3
"""Bounded BC/Q comparison on migrated reference actions; never publishes."""
import argparse
import json
from pathlib import Path
import sys
import time

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--run-prefix', required=True)
    p.add_argument('--steps', type=int, default=256)
    p.add_argument('--variant', choices=('both', 'bc', 'q'), default='both')
    p.add_argument('--project-critic-rotation', action='store_true')
    p.add_argument('--seed',type=int)
    p.add_argument('--bc-coverage', choices=('executed_prefix_v1', 'full_reference_v2', 'prefix_plus_reference_tail_v2'), default='executed_prefix_v1')
    p.add_argument('--critic-warmup', type=int, default=0)
    p.add_argument('--q-ramp', type=int, default=0)
    p.add_argument('--group', default='18_RLT_Objective_RTC_V41')
    p.add_argument('--conservative-q-weight', type=float, default=0.)
    args = p.parse_args()
    import torch
    import wandb
    from f1_toys.rlt_online import LocalLearner, sha, atomic_json
    from f1_toys.rlt_action_contract import REFERENCE, DECISION
    from f1_toys.rlt_demo_seed import load_demo_seed
    from f1_toys.rlt_critic_calibration import concatenate, subset, load_terminals, critic_report
    from f1_toys.rlt_evaluation import action_metrics, grouped_action_metrics
    from f1_toys.rlt_training_batch import sample_training_batch, SAMPLER
    if args.steps <= 0:
        raise ValueError('Positive bounded update budget required')
    cfg = json.loads((args.data/'candidate-data-config.json').read_text())['f1']
    migration = json.loads((args.data/'migration.json').read_text())
    representation=migration['action_representation']
    assert representation in (REFERENCE,DECISION)
    if representation==DECISION:assert migration['decision_anchor_migration'] is True
    torch.set_num_threads(2)
    parts = {s: [] for s in ('train', 'validation')}
    episodes = {s: set() for s in parts}
    validation_ids=[]
    for entry in migration['sources']:
        root = Path(entry['cache'])
        meta = json.loads((root/'manifest.json').read_text())
        assert meta['action_representation'] == representation
        for name, digest in meta['files_sha256'].items():
            assert Path(name).name == name and sha(root/name) == digest
        assert meta['split'] == entry['split'] and meta['episode_id'] == entry['episode']
        parts[meta['split']].append(torch.load(root/'replay.pt', weights_only=True))
        episodes[meta['split']].add(meta['episode_id'])
        if meta['split']=='validation':validation_ids += [meta['episode_id']]*meta['transition_count']
    assert not episodes['train'] & episodes['validation']
    local = {s: concatenate(v) for s, v in parts.items()}
    demos, demo_rows, demo_manifest = load_demo_seed(cfg['demo_seed'], cfg['replay']['feature_identity'])
    terminal, terminal_manifest = load_terminals(cfg['critic_terminal_cache'], cfg['replay']['feature_identity'])
    assert demo_manifest['action_representation'] == terminal_manifest['action_representation'] == representation
    validation_ids += [r['source_run'] for r in demo_rows['validation']]
    all_episodes={s:episodes[s] | {r['source_run'] for r in demo_rows[s]} |
                    {r['episode'] for r in terminal_manifest['provenance'][s]} for s in episodes}
    assert not all_episodes['train'] & all_episodes['validation'], 'Cross-pipeline validation leakage'
    validation = concatenate([local['validation'], demos['validation']])
    critic_validation = concatenate([validation, terminal['validation']])
    args.output.mkdir(parents=True, exist_ok=False)
    identity = dict(migration_sha256=sha(args.data/'migration.json'),
                    config_sha256=sha(args.data/'candidate-data-config.json'),
                    action_representation=representation, decision_anchor_migration=migration.get('decision_anchor_migration',False),
                    train_episodes=sorted(all_episodes['train']),validation_episodes=sorted(all_episodes['validation']),
                    local_counts={s:len(local[s]['actions']) for s in local},
                    demo_counts={s:len(demos[s]['actions']) for s in demos},
                    initial_actor_manifest_sha256=sha(Path(cfg['initial_head'])/'manifest.json'),
                    code_sha256={f.name: sha(f) for f in (TASK/'src/f1_toys').glob('rlt*.py')},
                    script_sha256=sha(__file__),
                    scope='development comparison; fixed split; no robot success claim; no deployment')
    atomic_json(args.output/'data-identity.json', identity)
    results = []
    warmup_digest = None
    for enabled in (False, True):
        if args.variant != 'both' and enabled != (args.variant == 'q'):
            continue
        variant = ('decision' if representation==DECISION else 'reference')+('-q' if enabled else '-bc')
        recipe = dict(cfg['training'], action_representation=representation,
                      enable_q_guidance=enabled, gradient_diagnostics=True,
                      actor_bc_coverage=args.bc_coverage, critic_warmup_steps=args.critic_warmup,
                      q_ramp_steps=args.q_ramp, replay_sampler=SAMPLER)
        recipe['conservative_q_weight'] = args.conservative_q_weight
        if args.seed is not None:recipe['seed']=args.seed
        if args.project_critic_rotation:
            recipe['critic_rotation_projection'] = 'serving_gram_schmidt_v1'
        torch.manual_seed(recipe['seed'])
        learner = LocalLearner(cfg['core_root'], recipe, cfg['initial_head'])
        generator = torch.Generator().manual_seed(recipe['seed'])
        run = wandb.init(project='f1_complexytoys', entity='robotics-example',
                         id=args.run_prefix+'-'+variant, group=args.group,
                         job_type='offline-contract-comparison',
                         tags=['f1', 'cpu', 'frozen-vla', 'no-deploy', variant],
                         dir=str(args.output), config=dict(recipe=recipe, data=identity, steps=args.steps))
        history = []
        try:
            initial = action_metrics(learner.model, validation)
            initial_grouped=grouped_action_metrics(learner.model,validation,validation_ids)
            atomic_json(args.output/(variant+'-initial-groups.json'),initial_grouped)
            run.log({'initial/'+k: v for k,v in initial.items()}, step=0)
            for step in range(args.steps):
                started = time.monotonic()
                batch, sampling = sample_training_batch(local['train'], demos['train'],
                    recipe['batch_size'], float(cfg.get('demo_fraction', .5)), generator=generator)
                anchors = subset(terminal['train'], torch.randint(len(terminal['train']['actions']),
                    (recipe['batch_size'],), generator=generator))
                metrics = learner.update(batch, terminal_batch=anchors)
                metrics['updates_per_second'] = 1/(time.monotonic()-started)
                if step == 0 or (step+1)%32 == 0 or step+1 == args.steps:
                    report = action_metrics(learner.model, validation)
                    critic = critic_report(learner, critic_validation)
                    metrics.update({'validation/'+k:v for k,v in report.items()})
                    metrics.update({'critic_validation/'+k:v for k,v in critic['metrics'].items() if v is not None})
                    history.append(dict(step=step+1, actions=report, critic=critic))
                    print(variant, step+1, json.dumps(history[-1]), flush=True)
                run.log(metrics, step=step+1)
                if step+1 == args.critic_warmup:
                    from f1_toys.rlt_evaluation import actor_digest
                    import hashlib
                    digest_state = hashlib.sha256()
                    for key, tensor in sorted(learner.critic.state_dict().items()):
                        digest_state.update(key.encode()); digest_state.update(tensor.detach().cpu().numpy().tobytes())
                    critic_digest = digest_state.hexdigest()
                    if warmup_digest is not None and critic_digest != warmup_digest:
                        raise ValueError('BC/Q critic warmup is not identical')
                    warmup_digest = critic_digest
                    atomic_json(args.output/(variant+'-warmup.json'), dict(
                        step=learner.step, q_updates=learner.q_updates,
                        actor_sha256=actor_digest(learner.model), critic_sha256=critic_digest))
                if step+1 in {128,256,512,1024} and step+1!=args.steps:
                    learner.save(args.output/variant/('step-'+str(step+1)),dict(data=identity,recipe=recipe))
            dest = args.output/variant
            digest = learner.save(dest, dict(data=identity, recipe=recipe))
            atomic_json(dest/'history.json', history)
            grouped=grouped_action_metrics(learner.model,validation,validation_ids)
            atomic_json(dest/'validation-groups.json',grouped)
            run.summary.update(dict(checkpoint_sha256=digest, deployed=False,
                                    critic_updates=learner.step, q_actor_updates=learner.q_updates))
            results.append(dict(variant=variant, initial=initial, final=history[-1], grouped=grouped, run_url=run.url))
        finally:
            run.finish()
    atomic_json(args.output/'comparison.json', dict(data=identity, results=results, deployed=False))


if __name__ == '__main__':
    main()
