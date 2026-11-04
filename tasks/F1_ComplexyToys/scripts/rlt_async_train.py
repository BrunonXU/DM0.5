#!/usr/bin/env python3
"""Launch F1's CPU small-head adapters on the pinned RLinf async runner."""
import argparse
import json
import os
from pathlib import Path
import sys

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    os.environ['F1_RLT_TASK_SRC'] = str(TASK/'src')
    from omegaconf import OmegaConf
    cfg = OmegaConf.load(args.config)
    cfg.f1.run_id = args.run_id
    from f1_toys.rlt_online import sha
    cfg.f1.source_sha256 = {name: sha(TASK/'src/f1_toys'/name) for name in (
        'rlt_online.py', 'rlt_async_workers.py', 'rlt_local_pipeline.py', 'rlt_local_replay.py',
        'rlt_snapshots.py', 'rlt_stage2.py', 'rlt_data_policy.py', 'rlt_evaluation.py', 'rlt_conditioning.py',
        'rlt_critic_calibration.py', 'rlt_demo_seed.py', 'rlt_online_bootstrap.py', 'rlt_runner.py',
        'rlt_admission.py', 'rlt_priority.py', 'rlt_action_contract.py',
        'rlt_decision_replay.py','rlt_prediction_index.py','rlt_training_batch.py','rlt_prefix.py',
        'rlt_supervision.py','rlt_transition_ledger.py','rlt_task_evaluation.py')}
    cfg.f1.launcher_sha256 = sha(Path(__file__))
    cfg.f1.initial_manifest_sha256 = sha(Path(cfg.f1.initial_head)/'manifest.json')
    if 'wandb' not in cfg.runner.logger.logger_backends:
        raise ValueError('Every training run requires W&B')
    os.environ['WANDB_RUN_ID'] = args.run_id
    os.environ['WANDB_RUN_GROUP'] = cfg.f1.get('wandb_group', '10_RLT_Local_Async_B1250')
    synthetic = cfg.f1.get('preflight_synthetic', False)
    os.environ['WANDB_JOB_TYPE'] = cfg.f1.get('wandb_job_type', 'synthetic-async-preflight' if synthetic else 'online-shadow' if not cfg.f1.publish_updates else 'online-local-actor')
    os.environ['WANDB_TAGS'] = 'f1,b1250,frozen-vla,placement-release,rlinf-async' + (',synthetic,no-robot,no-deploy' if synthetic else '')
    os.environ['WANDB_RESUME'] = 'allow'
    sys.path.insert(0, str(Path(cfg.f1.core_root)))
    os.environ['PYTHONPATH'] = os.pathsep.join([str(TASK/'src'), str(cfg.f1.core_root)])
    from rlinf.scheduler import Cluster, Channel, NodePlacementStrategy
    from f1_toys.rlt_runner import F1AsyncRunner
    from f1_toys.rlt_async_workers import F1EnvWorker, F1RolloutWorker, F1ActorWorker
    online_trial = cfg.f1.get('promotion_policy') == 'supervised_online_v1'
    if online_trial and (not cfg.f1.get('kinematic_gate', True) or not cfg.f1.get('demo_seed')
                         or not cfg.f1.training.get('stochastic_actor', False)):
        raise ValueError('Supervised online trials require demo provenance, sampling and IK checks')
    if cfg.f1.training.enable_q_guidance and not online_trial:
        audit = json.loads(Path(cfg.f1.q_guidance_audit).read_text())
        resume = cfg.runner.get('resume_dir')
        if (audit.get('passed') is not True or audit.get('objective') != 'approach_place_release_v1'
                or not resume or audit.get('checkpoint_sha256') != sha(Path(resume)/'actor/learner.pt')
                or audit.get('feature_identity') != OmegaConf.to_container(cfg.f1.replay.feature_identity, resolve=True)):
            raise ValueError('Q guidance requires the corrected-objective critic audit')
    if not synthetic:
        from f1_toys.rlt_data_policy import load_policy
        cfg.f1.data_policy_sha256 = sha(Path(cfg.f1.data_policy))
        selection = load_policy(cfg.f1.data_policy, cfg.f1.data_policy_sha256)
        cfg.f1.replay.data_policy = cfg.f1.data_policy
        cfg.f1.replay.data_policy_sha256 = cfg.f1.data_policy_sha256
        expected = sorted(name for name, entry in selection['episodes'].items() if entry['split'] == 'validation')
        if sorted(cfg.f1.replay.validation_episodes) != expected:
            raise ValueError('Replay validation split disagrees with data selection')
    cluster = Cluster(cluster_cfg=cfg.cluster)
    weights = Channel.create('F1LocalActorSnapshots')
    placement = NodePlacementStrategy([0], node_group_label='f1_cpu')
    actor = F1ActorWorker.create_group(cfg, weights).launch(cluster, name='actor', placement_strategy=placement)
    rollout = F1RolloutWorker.create_group(cfg, weights).launch(cluster, name='rollout', placement_strategy=placement)
    env = F1EnvWorker.create_group(cfg).launch(cluster, name='env', placement_strategy=placement)
    runner = F1AsyncRunner(cfg, actor, rollout, env, reward=None)
    runner.init_workers()
    try:
        runner.run()
    finally:
        runner.metric_logger.finish()


if __name__ == '__main__':
    main()
