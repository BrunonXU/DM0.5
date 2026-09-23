"""Single-GPU real base+new LoRA forward/backward/generation, no optimizer step."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--base-model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['WORLD_SIZE'] = '1'
    import torch
    import numpy as np
    from transformers import AutoProcessor, set_seed
    from opendm.model.dm05.dm05_lora import unwrap_dm05_model
    from .experiment import F1Exp
    from .execution import ActionDecoder
    started = time.monotonic()
    set_seed(42)
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    with tempfile.TemporaryDirectory(prefix='f1-model-check-') as temporary:
        exp = F1Exp(args.release, args.base_model, temporary, engineering=True).configure_shared()
        # Worker count is a smoke runtime setting; batch4 and all model settings remain recipe values.
        exp.trainer_config.dataloader_num_workers = 0
        exp.trainer_config.dataloader_persistent_workers = False
        exp.trainer_config.dataloader_prefetch_factor = None
        print('Loading unchanged native32 DM0.5 base and a fresh F1 LoRA', flush=True)
        model = exp.model_config.build_model(use_lora=True).to('cuda')
        base = unwrap_dm05_model(model)
        base.config.use_cache = False
        base.model.vlm.config.use_cache = False
        processor = AutoProcessor.from_pretrained(args.base_model, local_files_only=True)
        trainer = exp.build_trainer(model, processor)
        batch = next(iter(trainer.get_train_dataloader()))
        batch = trainer._prepare_inputs(batch)
        assert batch['action'].shape == (4, 50, 32)
        assert batch['action_mask'].dtype == torch.bool
        assert not batch['action_mask'][..., 23:].any()
        model.train()
        print('Running shared flow-matching forward/backward, batch4; no optimizer step', flush=True)
        with trainer.accelerator.autocast():
            loss = trainer.compute_loss(model, batch)
        assert torch.isfinite(loss)
        loss.backward()
        gradients = [(n, p.grad) for n, p in model.named_parameters() if p.requires_grad and p.grad is not None]
        assert gradients and all(torch.isfinite(g).all() for _, g in gradients)
        assert all(p.grad is None for p in model.parameters() if not p.requires_grad)
        groups = {}
        for name, markers in {'lora': ('lora_',), 'action_projections': ('action_in_proj', 'action_out_proj'),
                              'time_modules': ('time_mlp', 'time_modulator'),
                              'vision': ('vision_tower',), 'language': ('language_model',),
                              'action_expert': ('action_expert',)}.items():
            selected = [(n,g) for n,g in gradients if any(m in n for m in markers)]
            groups[name] = {'gradient_tensors': len(selected), 'nonzero_tensors': sum(bool((g != 0).any()) for _, g in selected)}
            assert groups[name]['nonzero_tensors'] > 0, (name, groups)
        loss_value = float(loss.detach())
        model.zero_grad(set_to_none=True)
        del loss, batch
        torch.cuda.empty_cache()
        print('Generating one validation observation, 10 Euler steps, fresh untrained adapter', flush=True)
        from opendm.data.collator import TrainingCollator
        dataset = exp.release.dataset('validation', processor)
        inputs = TrainingCollator(processor.tokenizer.pad_token_id)([dataset[0]])
        inputs = {k:v.to('cuda') for k,v in inputs.items() if k != 'action'}
        model.eval()
        with torch.no_grad(), trainer.accelerator.autocast():
            generated = base.inference_action(**inputs, diffusion_steps=10)
        prediction = generated[0].float().cpu().numpy()
        assert prediction.shape == (50, 32) and np.isfinite(prediction).all()
        epidx, start = dataset.index[0]
        raw = dataset.episodes[epidx].sample(start)
        decoded = ActionDecoder(exp.release.normalizer).decode(prediction, raw['state'][:14])
        ik = {k:decoded[k] for k in ('success', 'reason', 'step', 'arm') if k in decoded}
        if not decoded['success']:
            assert decoded['joint_targets_deg'] is None and 'base_velocity_targets' not in decoded
        trainable = json.loads((Path(temporary) / 'trainable_parameters.json').read_text())
        report = {'status': 'passed', 'scope': 'single-GPU full-model engineering check, not a trained policy',
                  'formal_training_started': False, 'optimizer_steps': 0, 'weights_saved': False,
                  'fresh_lora_from_base': True, 'batch_shape': [4,50,32], 'fm_loss': loss_value,
                  'gradient_groups': groups, 'all_observed_gradients_finite': True,
                  'trainable_total': trainable['trainable_total'], 'frozen_total': trainable['frozen_total'],
                  'unexpected_trainable_parameters': trainable['unexpected_trainable_parameters'],
                  'generation_shape': list(prediction.shape), 'generation_steps': 10,
                  'untrained_output_ik': ik, 'gpu_max_allocated_gib': torch.cuda.max_memory_allocated() / 1024**3,
                  'elapsed_seconds': time.monotonic()-started, 'binding': exp.binding}
        assert not trainable['unexpected_trainable_parameters']
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({k:v for k,v in report.items() if k != 'binding'}, indent=2), flush=True)


if __name__ == '__main__':
    main()
