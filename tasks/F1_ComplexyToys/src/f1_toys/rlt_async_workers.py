"""F1 adapters for the pinned RLinf AsyncEmbodiedRunner.

F1 retains the only physical control owner. Env consumes durable local receipts;
Rollout calls the existing frozen feature service; Actor trains only small heads.
No worker creates a robot publisher or performs an environment reset.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

import torch
from omegaconf import OmegaConf
from rlinf.scheduler import Worker

from .rlt_online import LocalLearner, atomic_json, sha
from .rlt_local_pipeline import prepare_local_cache, FrozenFeatureClient, replay_adapter_identity
from .rlt_snapshots import publish_actor, snapshot_name
from .rlt_data_policy import load_policy, episode_selection, select_receipt
from .rlt_evaluation import evaluate_candidate, kinematic_screen
from .rlt_admission import record_admission


async def _receive_when_available(worker, channel, *, poll_seconds=.5):
    """Wait for human-paced data without posting an indefinite Gloo receive.

    The pinned Channel.get(async_op=True) posts recv immediately, even when its
    queue is empty. A long operator break then reaches the process-group timeout.
    get_nowait returns an explicit QueueEmpty reply instead; only that condition
    is retried. Transport failures must remain visible to the worker caller.
    """
    while not worker.stopping:
        try:
            return channel.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(poll_seconds)
    raise asyncio.CancelledError


class F1EnvWorker(Worker):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.stopping = False
        self.seen = set()
        self.task = None

    def init_worker(self):
        Path(self.cfg.f1.cache_root).mkdir(parents=True, exist_ok=True)
        self.selection = None
        if not self.cfg.f1.get('preflight_synthetic', False):
            self.selection = load_policy(self.cfg.f1.data_policy, self.cfg.f1.data_policy_sha256)

    async def interact(self, input_channel, rollout_channel, reward_channel, actor_channel, metric_channel):
        self.task = asyncio.current_task()
        try:
            root = Path(self.cfg.f1.raw_root)
            while not self.stopping:
                # The same immutable receipt may arrive by either lane. A
                # successful revision is sent to Actor once, regardless of path.
                priority = self.cfg.f1.get('priority_root')
                receipts = (sorted(Path(priority).glob('*/local_segments/placement-1.json')) if priority else [])
                receipts += sorted(root.glob('*/local_segments/placement-1.json'))
                for receipt in receipts:
                    if receipt.parent.parent.name.startswith('.'):
                        continue
                    episode_id = json.loads(receipt.read_text())['episode_id']
                    digest = sha(receipt)
                    if self.selection is not None:
                        split = episode_selection(self.selection, receipt)
                        if split not in ('train', 'validation'):
                            atomic_json(Path(self.cfg.f1.cache_root)/('selection-'+episode_id+'.json'),
                                dict(episode_id=episode_id, decision=split, source_receipt_sha256=digest))
                            continue
                    review_root = self.cfg.f1.replay.get('reviews_root')
                    if review_root:
                        review = Path(review_root)/(episode_id+'.json')
                        if review.exists():
                            digest += sha(review)
                    if digest in self.seen:
                        continue
                    await rollout_channel.put(str(receipt.parent.parent), async_op=True).async_wait()
                    result = await input_channel.get(async_op=True).async_wait()
                    if result.get('cache'):
                        await actor_channel.put(result['cache'], async_op=True).async_wait()
                        self.seen.add(digest)
                        value = json.loads((Path(result['cache'])/'manifest.json').read_text())['local_result']
                        metrics = dict(local_segments_received=len(self.seen),
                            autonomous_success=float(value['status'] == 'success' and not value['assisted']),
                            assisted_success=float(value['status'] == 'success' and value['assisted']),
                            intervention=float(value['assisted']),
                            local_seconds=(value['ended_ns']-value['started_ns'])/1e9)
                        await metric_channel.put(dict(rank=0, env={k: torch.tensor([v]) for k, v in metrics.items()}), async_op=True).async_wait()
                    elif result.get('pending_review'):
                        self.seen.add(digest)
                    if self.stopping:
                        break
                await asyncio.sleep(1.)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self.stopping = True
        if self.task is not None:
            self.task.cancel()


class F1RolloutWorker(Worker):
    def __init__(self, cfg, weight_channel):
        super().__init__()
        self.cfg, self.weight_channel = cfg, weight_channel
        self.stopping = False
        self.task = None

    def init_worker(self):
        self.recipe = OmegaConf.to_container(self.cfg.f1.replay, resolve=True)
        self.client = FrozenFeatureClient(self.cfg.f1.feature_endpoint, self.recipe['feature_identity'])

    async def generate(self, input_channel, output_channel, metric_channel):
        self.task = asyncio.current_task()
        try:
            while not self.stopping:
                run = await _receive_when_available(self, input_channel)
                receipt = json.loads((Path(run)/'local_segments/placement-1.json').read_text())
                review_root = self.recipe.get('reviews_root')
                has_review = review_root and (Path(review_root)/(receipt['episode_id']+'.json')).exists()
                revision = sha(Path(run)/'local_segments/placement-1.json') + (
                    sha(Path(review_root)/(receipt['episode_id']+'.json')) if has_review else '')
                def receipt_status(status, **details):
                    record_admission(self.cfg.f1.get('health_root'), receipt['episode_id'],
                        'rollout', status, source_revision=revision, **details)
                if receipt['local_result']['status'] in ('interrupted', 'pending_review') and not has_review:
                    result = dict(pending_review=True)
                    receipt_status('pending_review')
                else:
                    try:
                        receipt_status('preparing')
                        predictions=None
                        from .rlt_action_contract import DECISION
                        if self.recipe.get('action_representation')==DECISION:
                            from .rlt_local_replay import read_prefix
                            from .rlt_prediction_index import source_predictions
                            streams=await asyncio.to_thread(read_prefix,Path(run),receipt)
                            plans=[e['plan'] for e in streams['events']
                                   if e['event']=='rlt_plan_adopted' and e['plan']['phase']=='actor']
                            logs = self.cfg.f1.get('prediction_logs')
                            logs = list(logs) if logs is not None else self.cfg.f1.prediction_log
                            predictions=await asyncio.to_thread(source_predictions,logs,plans)
                        cache = await asyncio.to_thread(prepare_local_cache, run, self.cfg.f1.cache_root,
                                                        self.recipe, self.client,predictions=predictions)
                        result = dict(cache=str(cache))
                        receipt_status('prepared', cache=str(cache))
                    except Exception as exc:
                        result = dict(error=str(exc), retry=True)
                        receipt_status('rejected', error=str(exc), retry=True)
                        atomic_json(Path(self.cfg.f1.cache_root)/'last-error.json', dict(run=run, **result))
                await output_channel.put(result, async_op=True).async_wait()
        except asyncio.CancelledError:
            pass

    async def sync_model_from_actor(self):
        message = await self.weight_channel.get(async_op=True).async_wait()
        if message['approved']:
            path = Path(message['path'])
            if path.parent.resolve() != Path(self.cfg.f1.snapshot_root).resolve():
                raise ValueError('Actor snapshot escaped shared registry')
            # A later candidate may already be published when this message is
            # received. Verify the immutable message, never move a pointer back.
            manifest = json.loads((path/'manifest.json').read_text())
            if (message.get('manifest_sha256') != sha(path/'manifest.json')
                    or not manifest.get('deployment_approved')
                    or manifest['head_sha256'] != sha(path/'head.safetensors')):
                raise ValueError('Invalid immutable actor sync receipt')
            atomic_json(path/'sync-receipt.json', dict(manifest_sha256=message['manifest_sha256'],
                        status='received', adopted=False))
        return message

    async def stop(self):
        self.stopping = True
        if self.task is not None:
            self.task.cancel()


class F1ActorWorker(Worker):
    def __init__(self, cfg, weight_channel):
        super().__init__()
        self.cfg, self.weight_channel = cfg, weight_channel
        self.stopping = False
        self.recv_task = None
        self.replay = {}
        self.received_transitions = 0
        self.credited_episodes = set()
        self.credited_transitions = set()
        self.validation = {}
        self.last_evaluation = None
        self.incumbent = None
        self.demo_data = self.demo_records = self.demo_manifest = None
        self.budget_origin_transitions = 0
        self.revision_conflict = None

    def init_worker(self):
        torch.set_num_threads(int(self.cfg.f1.cpu_threads))
        if not 0 <= float(self.cfg.f1.get('demo_fraction', .5)) <= 1:
            raise ValueError('Demo sampling fraction must be in [0,1]')
        torch.manual_seed(int(self.cfg.f1.training.seed))
        recipe = OmegaConf.to_container(self.cfg.f1.training, resolve=True)
        from .rlt_action_contract import SUBMITTED
        if recipe.get('action_representation', SUBMITTED) != self.cfg.f1.replay.get('action_representation', SUBMITTED):
            raise ValueError('Actor/Q and replay use different controller action representations')
        self.learner = LocalLearner(self.cfg.f1.core_root, recipe, self.cfg.f1.initial_head, device='cpu')
        self.critic_anchors = self.critic_anchor_manifest = None
        if self.cfg.f1.get('critic_terminal_cache'):
            from .rlt_critic_calibration import load_terminals
            self.critic_anchors, self.critic_anchor_manifest = load_terminals(
                self.cfg.f1.critic_terminal_cache,
                OmegaConf.to_container(self.cfg.f1.replay.feature_identity,resolve=True))
            if self.critic_anchor_manifest.get('action_representation', SUBMITTED) != recipe.get('action_representation', SUBMITTED):
                raise ValueError('Critic terminal actions require explicit representation migration')
        self.baseline = copy.deepcopy(self.learner.model).eval().requires_grad_(False)
        self.selection = None
        if not self.cfg.f1.get('preflight_synthetic', False):
            self.selection = load_policy(self.cfg.f1.data_policy, self.cfg.f1.data_policy_sha256)
        self.initial_manifest = json.loads((Path(self.cfg.f1.initial_head)/'manifest.json').read_text())
        compatibility = {k: v for k, v in recipe.items() if k != 'enable_q_guidance'}
        self.identity = dict(recipe_sha256=hashlib.sha256(json.dumps(compatibility, sort_keys=True).encode()).hexdigest(),
                             code_sha256=OmegaConf.to_container(self.cfg.f1.source_sha256, resolve=True),
                             sampling=dict(demo_fraction=float(self.cfg.f1.get('demo_fraction', .5)),
                                           updates_per_transition=int(self.cfg.f1.get('updates_per_transition', 4))),
                             data_policy_sha256=self.cfg.f1.get('data_policy_sha256'),
                             initial_head_sha256=sha(Path(self.cfg.f1.initial_head)/'manifest.json'),
                             feature_identity=OmegaConf.to_container(self.cfg.f1.replay.feature_identity, resolve=True))
        if self.critic_anchors is not None:
            self.identity['critic_terminal_manifest_sha256']=sha(Path(self.cfg.f1.critic_terminal_cache)/'manifest.json')
        if self.cfg.f1.get('demo_seed'):
            from .rlt_demo_seed import load_demo_seed
            self.demo_data, self.demo_records, self.demo_manifest = load_demo_seed(
                OmegaConf.to_container(self.cfg.f1.demo_seed, resolve=True), self.identity['feature_identity'])
            self.identity['demo_seed_manifest_sha256'] = self.cfg.f1.demo_seed.manifest_sha256
            if self.demo_manifest.get('action_representation', SUBMITTED) != recipe.get('action_representation', SUBMITTED):
                raise ValueError('Demo actions require explicit migration to the learner representation')
        if self.cfg.f1.get('bootstrap_checkpoint'):
            from .rlt_online_bootstrap import warm_start
            spec = OmegaConf.to_container(self.cfg.f1.bootstrap_checkpoint, resolve=True)
            warm_start(self.learner, spec, self.identity['feature_identity'], self.identity['initial_head_sha256'])
            self.identity['bootstrap_checkpoint'] = spec
        if self.cfg.f1.get('initial_update_budget') is not None:
            self.identity['initial_update_budget'] = int(self.cfg.f1.initial_update_budget)
        self.identity['promotion_policy'] = self.cfg.f1.get('promotion_policy', 'offline_nonregression_v1')
        if self.cfg.f1.get('preflight_synthetic', False) and self.cfg.f1.publish_updates:
            raise ValueError('Synthetic preflight can never publish an actor')
        # A new process must compare to what is actually offered to the robot,
        # not forget the incumbent and permit a chain of small regressions.
        pointer = Path(self.cfg.f1.snapshot_root)/'latest.json'
        if pointer.exists():
            meta = json.loads(pointer.read_text()); root = pointer.parent.resolve()
            path = (root/meta['directory']).resolve()
            if path.parent != root or sha(path/'manifest.json') != meta['manifest_sha256']:
                raise ValueError('Invalid incumbent snapshot pointer')
            manifest = json.loads((path/'manifest.json').read_text())
            if (manifest.get('deployment_approved') is not True
                    or sha(path/'head.safetensors') != manifest['head_sha256']
                    or any(manifest.get(k) != self.initial_manifest.get(k) for k in
                           ('model_config', 'action_norm_stats', 'parent_bundle_sha256', 'encoder_manifest_sha256'))):
                raise ValueError('Incumbent is not compatible with learner')
            from safetensors.torch import load_file
            self.incumbent = self._native_actor(load_file(str(path/'head.safetensors')))
        for cache in self.cfg.f1.get('seed_caches', []):
            self._ingest(cache)
        if self.cfg.f1.get('initial_update_budget') is not None:
            self.budget_origin_transitions = self.received_transitions
        self._health('waiting_for_data')

    def _native_actor(self, weights):
        cfg = dict(self.learner.cfg); cfg.pop('input_conditioning', None)
        with torch.random.fork_rng(devices=[]):
            previous = LocalLearner(self.cfg.f1.core_root, cfg, self.cfg.f1.initial_head)
        state = previous.model.state_dict()
        expected = {k for k in state if k.startswith(('backbone.', 'actor_mean.'))}
        if set(weights) != expected or any(weights[k].shape != state[k].shape for k in expected):
            raise ValueError('Incumbent actor shape mismatch')
        state.update(weights); previous.model.load_state_dict(state)
        return previous.model.eval().requires_grad_(False)

    def training_state(self):
        return dict(step=self.learner.step,q_updates=self.learner.q_updates,
                    update_budget=self._update_budget())

    def _ingest(self, path):
        root = Path(path)
        manifest = json.loads((root/'manifest.json').read_text())
        from .rlt_action_contract import SUBMITTED
        if manifest.get('action_representation', SUBMITTED) != self.learner.cfg.get('action_representation', SUBMITTED):
            raise ValueError('Replay/controller action representation mismatch')
        if (manifest.get('schema') != 'f1_local_replay_cache_v1'
                or manifest.get('objective') != 'approach_place_release_v1'
                or manifest.get('learning_scope', 'local_replay') != 'local_replay'
                or manifest['feature_identity'] != self.identity['feature_identity']):
            raise ValueError('Replay identity mismatch')
        for name, expected in manifest['files_sha256'].items():
            if Path(name).name != name or sha(root/name) != expected:
                raise ValueError('Replay hash mismatch')
        if manifest.get('synthetic') and not self.cfg.f1.get('preflight_synthetic', False):
            raise ValueError('Synthetic replay excluded from real training')
        if not manifest.get('synthetic'):
            replay_recipe = OmegaConf.to_container(self.cfg.f1.replay, resolve=True)
            expected = hashlib.sha256(json.dumps(replay_recipe, sort_keys=True).encode()).hexdigest()
            if manifest.get('recipe_sha256') != expected or manifest.get('adapter_sha256') != replay_adapter_identity():
                raise ValueError('Replay recipe or adapter code changed; rebuild derived cache')
        if self.selection is not None:
            selected = select_receipt(self.selection, json.loads((root/'source-receipt.json').read_text()),
                                      manifest['source_receipt_sha256'])
            if selected not in ('train', 'validation') or selected != manifest['split']:
                raise ValueError('Replay not eligible or split disagrees with frozen selection')
        data = torch.load(root/'replay.pt', map_location='cpu', weights_only=True)
        previous = self.replay.get(manifest['episode_id'])
        if previous is not None and previous[1] != sha(root/'manifest.json') and self.learner.step:
            from .rlt_transition_ledger import retracted_ids
            old_rows = [json.loads(s) for s in (Path(previous[0])/'transitions.jsonl').read_text().splitlines()]
            new_rows = [json.loads(s) for s in (root/'transitions.jsonl').read_text().splitlines()]
            retracted = retracted_ids(old_rows,previous[2],new_rows,data)
            if retracted:
                self.revision_conflict = dict(episode=manifest['episode_id'], previous_manifest=previous[1],
                    new_manifest=sha(root/'manifest.json'), learner_step=self.learner.step,
                    retracted_transition_ids=sorted(retracted),
                    reason='trained_targets_retracted_requires_trusted_checkpoint_replay')
                atomic_json(Path(self.cfg.f1.health_root)/'revision-conflict.json', self.revision_conflict)
                return
        if self.demo_records is not None:
            other = 'train' if manifest['split'] == 'validation' else 'validation'
            if manifest['episode_id'] in {r['source_run'] for r in self.demo_records[other]}:
                raise ValueError('Demo/replay validation leakage')
        if manifest['split'] == 'validation':
            if self.critic_anchor_manifest and manifest['episode_id'] in {
                    r['episode'] for r in self.critic_anchor_manifest['provenance']['train']}:
                raise ValueError('Critic anchor episode leaked into validation replay')
            self.validation[manifest['episode_id']] = (str(root), sha(root/'manifest.json'), data)
            self._admitted(root, manifest)
            return
        if manifest['split'] != 'train':
            raise ValueError('Unknown replay split')
        if self.critic_anchor_manifest and manifest['episode_id'] in {
                r['episode'] for r in self.critic_anchor_manifest['provenance']['validation']}:
            raise ValueError('Critic validation episode leaked into training replay')
        if len(data['actions']) > self.cfg.f1.max_replay_transitions:
            raise ValueError('One local trajectory exceeds replay capacity')
        from .rlt_transition_ledger import new_credits
        records_path = root/'transitions.jsonl'
        if records_path.exists():
            records = [json.loads(line) for line in records_path.read_text().splitlines()]
            if len(records) != len(data['actions']):
                raise ValueError('Transition ledger/tensor count mismatch')
            added, ids = new_credits(self.credited_transitions, records)
            self.received_transitions += len(added)
            self.credited_transitions.update(ids)
        elif manifest.get('synthetic'):
            if manifest['episode_id'] not in self.credited_episodes:
                self.received_transitions += len(data['actions'])
        else:
            raise ValueError('Real replay requires transition-level budget evidence')
        self.credited_episodes.add(manifest['episode_id'])
        self.replay[manifest['episode_id']] = (str(root), sha(root/'manifest.json'), data)
        while sum(len(value[2]['actions']) for value in self.replay.values()) > self.cfg.f1.max_replay_transitions:
            del self.replay[next(iter(self.replay))]
        self._admitted(root, manifest)

    def _admitted(self, root, manifest):
        record_admission(self.cfg.f1.get('health_root'), manifest['episode_id'], 'actor', 'buffered',
            source_revision=manifest['source_receipt_sha256']+(manifest.get('review_sha256') or ''),
            split=manifest['split'], transitions=manifest['transition_count'], cache=str(root),
            manifest_sha256=sha(root/'manifest.json'), learner_step=self.learner.step,
            reward=manifest['local_result']['reward'], assisted=manifest['local_result']['assisted'])

    async def recv_rollout_trajectories(self, input_channel):
        self.recv_task = asyncio.current_task()
        try:
            while not self.stopping:
                path = await _receive_when_available(self, input_channel)
                self._ingest(path)
        except asyncio.CancelledError:
            pass

    def run_training(self):
        if self.revision_conflict:
            self._health('revision_requires_replay', self.revision_conflict)
            return {}
        if self.recv_task is not None and self.recv_task.done() and not self.stopping:
            error = self.recv_task.exception()
            self._health('failed', str(error or 'replay receiver stopped'))
            raise RuntimeError('Replay receiver stopped') from error
        if self.selection is not None and not self.validation:
            self._health('waiting_for_validation')
            return {}
        budget = self._update_budget()
        if self.learner.step >= budget:
            self._health('waiting_for_new_transitions')
            return {}
        values = [item[2] for item in self.replay.values()]
        if not values:
            return {}
        from .rlt_critic_calibration import concatenate
        data = concatenate(values)
        size = len(data['actions'])
        if size < self.cfg.f1.min_replay_transitions:
            return {}
        batch_size = int(self.cfg.f1.training.batch_size)
        # A separate expert sampling view of the same immutable transitions:
        # sparse corrections must not disappear in a long autonomous replay.
        from .rlt_critic_calibration import concatenate, subset
        from .rlt_training_batch import sample_training_batch
        batch, sampling = sample_training_batch(data,
            None if self.demo_data is None else self.demo_data['train'], batch_size,
            float(self.cfg.f1.get('demo_fraction', .5)))
        terminal_batch=None
        if self.critic_anchors is not None:
            from .rlt_critic_calibration import subset
            terminals=self.critic_anchors['train']
            terminal_batch=subset(terminals,torch.randint(len(terminals['actions']),(batch_size,)))
        metrics = dict(self.learner.update(batch,terminal_batch=terminal_batch), replay_size=size, demo_size=sampling['expert_size'],
                       demo_seed_size=0 if self.demo_data is None else len(self.demo_data['train']['actions']),
                       update_budget=budget,
                       demo_sample_fraction=sampling['expert_sample_fraction'])
        offset = 0
        selected = set(sampling['sampled_local_indices'])
        for episode, (path, digest, part) in self.replay.items():
            used = sorted(i-offset for i in selected if offset <= i < offset+len(part['actions']))
            offset += len(part['actions'])
            if used:
                record_admission(self.cfg.f1.get('health_root'), episode, 'learner', 'consumed',
                    manifest_sha256=digest, cache=path, transition_indices=used,
                    learner_step=self.learner.step, q_updates=self.learner.q_updates)
        if self.validation and self.learner.step % int(self.cfg.f1.get('evaluation_interval', 50)) == 0:
            self.last_evaluation = self._evaluate()
            metrics.update({'validation/'+k:v for k,v in self.last_evaluation['candidate'].items()})
            metrics['validation/passed'] = float(self.last_evaluation['passed'])
        self._health('training')
        return metrics

    def _update_budget(self):
        initial = int(self.cfg.f1.get('initial_update_budget', 0))
        return initial + (self.received_transitions-self.budget_origin_transitions)*int(self.cfg.f1.get('updates_per_transition', 4))

    def _health(self, status, error=None):
        root = self.cfg.f1.get('health_root')
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
            atomic_json(Path(root)/'actor.json', dict(status=status, updated_unix=time.time(), error=error,
                step=self.learner.step, q_updates=self.learner.q_updates, update_budget=self._update_budget(),
                received_transitions=self.received_transitions,
                train_transitions=sum(len(v[2]['actions']) for v in self.replay.values()),
                validation_transitions=sum(len(v[2]['actions']) for v in self.validation.values()),
                demo_train=0 if self.demo_data is None else len(self.demo_data['train']['actions']),
                demo_validation=0 if self.demo_data is None else len(self.demo_data['validation']['actions'])))

    def _evaluate(self):
        if not self.validation:
            raise ValueError('No held-out replay available for promotion')
        if set(self.validation) & set(self.replay):
            raise ValueError('Validation leaked into replay')
        values = [item[2] for item in self.validation.values()]
        if getattr(self, 'demo_data', None) is not None:
            values.append(self.demo_data['validation'])
        from .rlt_critic_calibration import concatenate
        data = concatenate(values)
        identity = dict(data_policy_sha256=self.cfg.f1.get('data_policy_sha256'),
            episodes={key: value[1] for key,value in self.validation.items()},
            training_episodes=sorted(self.replay))
        if getattr(self, 'demo_manifest', None) is not None:
            identity['demo_seed_manifest_sha256'] = self.identity['demo_seed_manifest_sha256']
        report = evaluate_candidate(self.learner.model, self.baseline, data, identity, self.incumbent)
        if self.learner.cfg.get('enable_q_guidance',False):
            from .rlt_critic_calibration import critic_report,concatenate,subset
            terminal_data=subset(data,data['terminated'])
            if self.critic_anchors is not None:
                terminal_data=concatenate([terminal_data,self.critic_anchors['validation']])
            report['critic']=critic_report(self.learner,terminal_data)
            report['checks']['current_critic_calibrated']=report['critic']['passed']
            report['passed']=all(report['checks'].values())
        if self.cfg.f1.get('kinematic_gate', True):
            records = [json.loads(line) for path, _, _ in self.validation.values()
                       for line in (Path(path)/'transitions.jsonl').read_text().splitlines()]
            if getattr(self, 'demo_records', None) is not None:
                records += self.demo_records['validation']
            report['kinematics'] = kinematic_screen(self.learner.model, self.incumbent or self.baseline,
                data, records, self.initial_manifest['action_norm_stats'])
            report['checks']['ik_nonregression'] = report['kinematics']['passed']
            report['passed'] = all(report['checks'].values())
        if self.cfg.f1.get('promotion_policy') == 'supervised_online_v1':
            from .rlt_evaluation import supervised_online_checks
            report = supervised_online_checks(report)
        return report

    async def sync_model_to_rollout(self):
        root = Path(self.cfg.f1.snapshot_root)
        evaluation = self._evaluate() if self.validation else None
        provenance = dict(run_id=self.cfg.f1.get('run_id'), learner_identity=self.identity,
            replay_catalog={key: {'manifest_sha256': item[1], 'split': split}
                for split, catalog in (('train', self.replay), ('validation', self.validation))
                for key, item in catalog.items()})
        target = root/snapshot_name(self.learner, evaluation, provenance)
        approved = bool(not self.revision_conflict and self.cfg.f1.publish_updates and self.learner.step > 0
                        and evaluation and evaluation['passed'])
        if not target.exists():
            target = publish_actor(self.learner, self.initial_manifest, root,
                                   deployment_approved=approved, evaluation=evaluation,
                                   provenance=provenance)
        else:
            # Restore/repeated synchronization reuses immutable prior decision.
            approved = bool(not self.revision_conflict and self.cfg.f1.publish_updates and json.loads((target/'manifest.json').read_text())['deployment_approved'])
        if approved:
            self.incumbent = copy.deepcopy(self.learner.model).eval().requires_grad_(False)
            if self.cfg.f1.get('promoted_checkpoint_root'):
                checkpoint = Path(self.cfg.f1.promoted_checkpoint_root)/target.name
                if not checkpoint.exists():
                    checkpoint.mkdir(parents=True)
                    self.save_checkpoint(checkpoint, self.learner.step)
        await self.weight_channel.put(dict(path=str(target), approved=approved,
            manifest_sha256=sha(target/'manifest.json')), async_op=True).async_wait()

    def save_checkpoint(self, path, step):
        if step != self.learner.step:
            raise ValueError('Runner/learner step mismatch')
        self.learner.save(path, self.identity)
        if self.incumbent is not None:
            from .rlt_conditioning import export_actor_state
            torch.save(export_actor_state(self.incumbent), Path(path)/'incumbent.pt')
        atomic_json(Path(path)/'replay-catalog.json', [{'path': p, 'sha256': digest} for p, digest, _ in (*self.replay.values(), *self.validation.values())])
        atomic_json(Path(path)/'data-budget.json', dict(received_transitions=self.received_transitions,
                                                     credited_transitions=sorted(self.credited_transitions),
                                                     credited_episodes=sorted(self.credited_episodes),
                                                     budget_origin_transitions=self.budget_origin_transitions))

    def load_checkpoint(self, path):
        self.learner.restore(path, self.identity)
        seeds = [entry[0] for entry in self.replay.values()]
        self.replay.clear()
        self.credited_transitions.clear()
        self.credited_episodes.clear()
        self.received_transitions = 0
        for entry in json.loads((Path(path)/'replay-catalog.json').read_text()):
            if sha(Path(entry['path'])/'manifest.json') != entry['sha256']:
                raise ValueError('Replay changed since checkpoint')
            self._ingest(entry['path'])
        if (Path(path)/'incumbent.pt').exists():
            # Native unconditioned copy of the last deployed model, not last train step.
            self.incumbent = self._native_actor(torch.load(Path(path)/'incumbent.pt', weights_only=True))
        budget = Path(path)/'data-budget.json'
        if budget.exists():
            value = json.loads(budget.read_text())
            self.received_transitions = value['received_transitions']
            # _ingest reconstructed IDs before restoring an old checkpoint's
            # original budget; rebuilding that cache cannot recredit it.
            self.credited_transitions.update(value.get('credited_transitions', []))
            self.credited_episodes = set(value['credited_episodes'])
            self.budget_origin_transitions = value.get('budget_origin_transitions', 0)
        # Apply current seeds only after restoring the ledger. This also
        # credits newly approved tails of an already-known episode exactly once.
        for seed in seeds:
            self._ingest(seed)

    async def stop(self):
        self.stopping = True
        if self.recv_task is not None:
            self.recv_task.cancel()
