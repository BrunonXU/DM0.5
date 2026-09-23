"""Real shared processor + Trainer/DataLoader smoke, no DM0.5 weights/optimizer steps."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--base-model', required=True)
    parser.add_argument('--processor', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    os.environ['WORLD_SIZE'] = '1'
    import torch
    from torch.utils.data import DataLoader
    from transformers import AutoProcessor
    from accelerate.data_loader import prepare_data_loader
    from .experiment import F1Exp
    from .data import TrainingDrawSampler
    torch.set_num_threads(2)
    processor = AutoProcessor.from_pretrained(args.processor, local_files_only=True)
    with tempfile.TemporaryDirectory(prefix='f1-trainer-cpu-') as temporary:
        exp = F1Exp(args.release, args.base_model, temporary, engineering=True).configure_shared()
        previous_rank = os.environ.get('RANK')
        try:
            os.environ['RANK'] = '1'
            exp.configure_shared()
            assert exp.model_config.lora_config.dump_trainable_path is None
        finally:
            if previous_rank is None:
                os.environ.pop('RANK', None)
            else:
                os.environ['RANK'] = previous_rank
            exp.configure_shared()
        # Test fixture only: production experiment.yaml remains bf16/TF32/4 GPU.
        exp.trainer_config.bf16 = False
        exp.trainer_config.tf32 = False
        exp.trainer_config.dataloader_num_workers = 0
        exp.trainer_config.dataloader_persistent_workers = False
        exp.trainer_config.dataloader_prefetch_factor = None

        class Placeholder(torch.nn.Module):
            def __init__(self):
                super().__init__(); self.fixture_parameter = torch.nn.Parameter(torch.ones(1))
            def forward(self, **kwargs):
                raise AssertionError('CPU data smoke must not invoke a training model')

        trainer = exp.build_trainer(Placeholder(), processor)
        loader = trainer.get_train_dataloader()
        loader.set_epoch(0)
        batch = next(iter(loader))
        assert batch['action'].shape == (4, 50, 32)
        assert batch['action_mask'].dtype == torch.bool
        assert not batch['action_mask'][..., 23:].any()
        assert torch.isfinite(batch['action']).all()
        assert isinstance(trainer._get_train_sampler(), TrainingDrawSampler)
        assert trainer.args.gradient_accumulation_steps == 2
        assert trainer.args.learning_rate == .0005
        assert trainer.args.adam_beta2 == .95
        assert trainer.args.lr_scheduler_kwargs == {'min_lr_rate': .1}
        assert trainer.args.max_steps == 100000 and trainer.args.save_total_limit == 20
        ds = trainer.train_dataset
        key = next(iter(TrainingDrawSampler(ds)))
        sample1 = ds[key]
        sample2 = ds[key]
        for name in ('pixel_values', 'input_ids', 'action', 'action_mask'):
            assert torch.equal(sample1[name], sample2[name]), name
        # Same draw identity reproduces augmentation in a fresh Dataset (resume).
        recreated = exp.release.dataset('train', processor, seed=42)
        assert torch.equal(sample1['pixel_values'], recreated[key]['pixel_values'])
        assert not torch.equal(sample1['pixel_values'], ds[(key[0], 1, 0)]['pixel_values'])
        validation = exp.release.dataset('validation', processor)
        assert torch.equal(validation[0]['pixel_values'], validation[0]['pixel_values'])
        # Exercise Accelerate's actual 4-way batch sharding, without process groups/GPU.
        class Identities:
            def __len__(self): return len(ds)
            def __getitem__(self, key): return torch.tensor(key)
        expected = list(TrainingDrawSampler(ds))[:16]
        observed = []
        for rank in range(4):
            raw = DataLoader(Identities(), batch_size=4, sampler=TrainingDrawSampler(ds), drop_last=True)
            shard = prepare_data_loader(raw, device=torch.device('cpu'), num_processes=4, process_index=rank,
                                        split_batches=False, put_on_device=False, even_batches=True)
            shard.set_epoch(0)
            observed.extend(tuple(x) for x in next(iter(shard)).tolist())
        assert observed == expected, (observed, expected)
        # Worker seeding/prefetch must not alter per-draw augmentation.
        from opendm.data.collator import TrainingCollator
        multi = DataLoader(exp.release.dataset('train', processor), batch_size=4,
                           sampler=TrainingDrawSampler(ds), num_workers=2,
                           collate_fn=TrainingCollator(processor.tokenizer.pad_token_id))
        worker_batch = next(iter(multi))
        assert torch.equal(batch['pixel_values'], worker_batch['pixel_values'])
        # Exercise generated-action evaluator plumbing with a label-returning fixture.
        # This validates isolation/reporting, not DM0.5 generation quality.
        from contextlib import nullcontext
        from types import SimpleNamespace
        from .evaluation import evaluate_model
        first_val = exp.release.for_split('validation')[0]
        exp.release.document['evaluation'] = {'diffusion_steps': 10, 'seeds': [42, 43],
            'samples': [{'episode': first_val.episode, 'start': int(first_val.starts[0])}]}
        label = exp.release.normalizer(first_val.sample(first_val.starts[0]))['action']
        class LabelFixture(torch.nn.Module):
            def forward(self, **batch):
                return SimpleNamespace(loss=torch.tensor(0.))
            def inference_action(self, **inputs):
                assert 'action' not in inputs and inputs['diffusion_steps'] == 10
                assert inputs['action_mask'].dtype == torch.bool
                return torch.tensor(label).unsqueeze(0)
        fixture = LabelFixture()
        rng_before = torch.get_rng_state().clone()
        generated = evaluate_model(fixture, exp.release, processor, Path(temporary) / 'evaluation',
                                   device='cpu', autocast=nullcontext)
        assert torch.equal(rng_before, torch.get_rng_state()) and fixture.training
        assert len(generated['records']) == 2
        assert all(r['ik']['success'] for r in generated['records'])
        assert generated['automatic_checkpoint_selection'] is False
        class StochasticFixture(LabelFixture):
            def forward(self, **batch):
                return SimpleNamespace(loss=torch.rand(37).mean())
            def inference_action(self, **inputs):
                result = super().inference_action(**inputs)
                result[..., 0] += torch.rand(50) * .0001
                return result
        fixture = StochasticFixture()
        stochastic = evaluate_model(fixture, exp.release, processor, Path(temporary) / 'stochastic_a',
                                    device='cpu', autocast=nullcontext)
        exp.release.document['evaluation']['flow_seeds'] = [99, 100, 101]
        independent = evaluate_model(fixture, exp.release, processor, Path(temporary) / 'stochastic_b',
                                     device='cpu', autocast=nullcontext)
        assert stochastic['records'] == independent['records']
        assert len(stochastic['flow_records']) == 2 and len(independent['flow_records']) == 3
        assert torch.equal(rng_before, torch.get_rng_state()) and fixture.training
        from .validation_protocol import build_recipe_selection
        stages = {stage: build_recipe_selection(exp.release, stage) for stage in ('pass_a', 'pass_b')}
        assert len(stages['pass_a']['samples']) == 8 and len(stages['pass_a']['flow_samples']) == 32
        assert len(stages['pass_b']['samples']) == 32
        report = {'status': 'passed', 'training_started': False, 'dm05_weights_loaded': False,
                  'gpu_used': False, 'optimizer_steps': 0, 'scope': 'real F1Trainer/DataLoader and processor; placeholder model never forwarded',
                  'source_counts': {s: len(exp.release.for_split(s)) for s in ('train', 'validation')},
                  'batch_shapes': {k: list(v.shape) for k,v in batch.items()},
                  'fixed_recipe_optimizer_arguments': True, 'rank0_only_trainable_summary': True, 'actual_accelerate_four_way_sharding': True,
                  'repeat_and_fresh_dataset_augmentation_match': True, 'zero_vs_two_workers_match': True,
                  'epoch_changes_augmentation': True, 'validation_deterministic': True,
                  'evaluation_label_fixture_and_rng_restoration': True,
                  'generation_independent_of_flow_seeds_and_rng_consumption': True,
                  'approved_pass_a_pass_b_manifest_counts': True, 'budget_100k_20_checkpoints': True,
                  'recipe_sha256': exp.binding['recipe_sha256']}
    with args.output.open('x') as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
