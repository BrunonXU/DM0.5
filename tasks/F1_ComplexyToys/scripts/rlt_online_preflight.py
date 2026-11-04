#!/usr/bin/env python3
"""Synthetic CPU preflight; never publishes usable actor weights or robot IO."""
import argparse
import copy
import json
from pathlib import Path
import sys

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    import torch
    import wandb
    from f1_toys.rlt_online import LocalLearner, atomic_json, sha
    from f1_toys.rlt_snapshots import publish_actor
    cfg = json.loads(args.config.read_text())
    training = cfg['f1']['training']
    torch.set_num_threads(2)
    torch.manual_seed(training['seed'])
    run = wandb.init(project='f1_complexytoys', entity='robotics-example',
        group='10_RLT_Local_Async_B1250', job_type='synthetic-cpu-preflight',
        tags=['f1', 'synthetic', 'no-robot', 'no-deploy'], id=args.run_id,
        config=cfg, dir=str(args.output))
    try:
        initial = cfg['f1']['initial_head']
        old_sha = sha(Path(initial)/'head.safetensors')
        learner = LocalLearner(cfg['f1']['core_root'], training, initial)
        n = 64
        def features():
            return dict(z_rl=torch.randn(n, 2048)*.05, proprio=torch.randn(n, 8)*.05,
                        ref_chunk=torch.randn(n, 50, 10)*.05)
        mask = torch.ones(n, 16, dtype=torch.bool); mask[:32, 7:] = False
        data = dict(obs=features(), next_obs=features(), actions=torch.randn(n, 16, 10)*.1,
                    action_time_mask=mask, intervene_flags=mask.clone(),
                    elapsed_steps=mask.sum(1).float()-.1, terminated=torch.ones(n, dtype=torch.bool),
                    reward=torch.arange(n).remainder(2).float(), discount=torch.full((n,), .99))
        manifest = dict(schema='f1_local_replay_cache_v1', objective='approach_place_release_v1',
            episode_id='synthetic-only', segment_id='synthetic-only:p', synthetic=True, split='train',
            feature_identity=cfg['f1']['replay']['feature_identity'], files_sha256={})
        cache = args.output/'synthetic-cache'; cache.mkdir()
        torch.save(data, cache/'replay.pt')
        manifest['files_sha256']['replay.pt'] = sha(cache/'replay.pt')
        atomic_json(cache/'manifest.json', manifest)
        baseline = copy.deepcopy(learner.model.actor_mean.state_dict())
        for _ in range(2):
            run.log(learner.update(data), step=learner.step)
        identity = dict(preflight=True, initial_sha256=old_sha)
        learner.save(args.output/'checkpoint', identity)
        restored = LocalLearner(cfg['f1']['core_root'], training, initial)
        restored.restore(args.output/'checkpoint', identity)
        a = learner.model.sac_forward(data['obs'], deterministic=True)[0]
        b = restored.model.sac_forward(data['obs'], deterministic=True)[0]
        torch.testing.assert_close(a, b, atol=0, rtol=0)
        assert any(not torch.equal(v, baseline[k]) for k, v in learner.model.actor_mean.state_dict().items())
        assert sha(Path(initial)/'head.safetensors') == old_sha
        initial_manifest = json.loads((Path(initial)/'manifest.json').read_text())
        snapshot = publish_actor(learner, initial_manifest, args.output/'snapshots', deployment_approved=False)
        assert not (snapshot.parent/'latest.json').exists()
        atomic_json(args.output/'acceptance.json', dict(synthetic=True, cpu_only=True,
            parameter_updates=learner.step, restored_actor_exact=True, base_export_unchanged=True,
            snapshot_advertised=False, robot_output=False, wandb_run_id=args.run_id))
        cfg['f1'].update(preflight_synthetic=True, seed_caches=[str(cache)],
            snapshot_root=str(args.output/'runner-snapshots'), raw_root=str(args.output/'empty-raw'),
            cache_root=str(args.output/'runner-cache'), publish_updates=False)
        cfg['runner'].update(max_epochs=2, max_steps=2, weight_sync_interval=1, save_interval=1)
        cfg['runner']['logger'].update(log_path=str(args.output/'runner-logs'), experiment_name='synthetic-cpu-preflight')
        atomic_json(args.output/'runner-config.json', cfg)
    finally:
        run.finish()


if __name__ == '__main__':
    main()
