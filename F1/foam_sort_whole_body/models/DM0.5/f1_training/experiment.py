"""F1 Exp + shared LoRA/Trainer integration. No implicit resume or stats fit."""
from __future__ import annotations
import json
import os
from pathlib import Path
from types import SimpleNamespace
import yaml
from f1_data.reader import require, sha256, SUPPORTED_CONTRACTS
from .data import Release

ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = ROOT / 'experiment.yaml'


def read_recipe():
    doc = yaml.safe_load(RECIPE_PATH.read_text())
    require(doc['policy_contract'] in SUPPORTED_CONTRACTS, 'recipe contract mismatch')
    require(doc['model']['action_dim'] == 32 and doc['model']['chunk_size'] == 50, 'reader requires H50/32D')
    return doc


def code_fingerprints():
    projects = ROOT.parents[3]
    paths = list((ROOT / 'f1_training').glob('*.py')) + list((ROOT / 'f1_data').glob('*.py'))
    shared = projects / 'DM0.5/opendm/opendm'
    paths += [shared / p for p in ('exp/dm05_exp.py', 'trainer/trainer.py', 'model/dm05/dm05_arch.py',
                                  'model/dm05/dm05_lora.py', 'data/augmentations.py', 'data/transforms.py', 'data/collator.py')]
    return {str(p.relative_to(projects)): sha256(p) for p in sorted(paths)}


class F1Exp:
    def __init__(self, release_path, base_model, output_dir, *, engineering=False):
        self.recipe_path = RECIPE_PATH
        self.recipe = read_recipe()
        self.release = Release(release_path, engineering=engineering)
        require(engineering or self.recipe['policy_contract'] == self.release.document['contract'], 'recipe/release contract mismatch')
        self.base_model, self.output_dir = str(Path(base_model).resolve()), str(Path(output_dir).resolve())
        self.engineering = engineering
        require(not (Path(base_model) / 'adapter_config.json').exists(), 'initialize from DM0.5 base, not a G2/F1 adapter')
        config = json.loads((Path(base_model) / 'config.json').read_text())
        require(config.get('action_dim') == 32 and config.get('chunk_size') == 50, 'base must be native DM0.5 H50/32D')
        self.binding = {'recipe_sha256': sha256(RECIPE_PATH), 'release_sha256': sha256(self.release.path),
                        'statistics_sha256': sha256(self.release.stats_path), 'code': code_fingerprints(),
                        'base_model': self.base_model, 'base_config_sha256': sha256(Path(base_model) / 'config.json'),
                        'ik_version': self.recipe['ik_recipe_version'], 'seed': self.recipe['reproducibility']['seed']}
        self.use_lora = True
        self.data_config = SimpleNamespace(norm_stats_path=lambda horizon: self.release.stats_path)

    def configure_shared(self):
        from opendm.exp.dm05_exp import DM05ModelConfig, DM05OptimizerConfig, DM05TrainerConfig
        from opendm.model.dm05.dm05_lora import DM05LoraConfig
        self.model_config = DM05ModelConfig(model_name_or_path=self.base_model, **self.recipe['model'])
        self.model_config.lora_config = DM05LoraConfig(**self.recipe['lora'], dump_trainable_path=(str(Path(self.output_dir) / 'trainable_parameters.json') if int(os.environ.get('RANK', '0')) == 0 else None))
        self.optimizer_config = DM05OptimizerConfig(**self.recipe['optimizer'])
        self.trainer_config = DM05TrainerConfig(output_dir=self.output_dir, **self.recipe['trainer'])
        self.trainer_config.seed = self.recipe['reproducibility']['seed']
        if self.engineering:
            self.trainer_config.wandb_project = ''  # Engineering checks must not create formal runs.
        return self

    def build_trainer(self, model, processor):
        from opendm.data.collator import TrainingCollator
        from .trainer import F1Trainer
        dataset = self.release.dataset('train', processor, seed=self.trainer_config.seed)
        return F1Trainer(model=model, processing_class=processor, exp_config=self, train_dataset=dataset,
                         data_collator=TrainingCollator(processor.tokenizer.pad_token_id, max_length=1024))

    def train(self, *, resume=None):
        from .evaluation import validate_selection
        require(not self.engineering, 'engineering manifest cannot launch training')
        selection = validate_selection(self.release)
        from .validation_protocol import build_recipe_selection
        require(selection == build_recipe_selection(self.release, 'pass_a'), 'formal training must bind the approved Pass A protocol')
        require(self.recipe['evaluation'].get('checkpoint_weighting_status') != 'pending_user_confirmation', 'checkpoint weighting awaits user confirmation')
        require(self.recipe['evaluation']['selection_protocol'] != 'pending_user_confirmation', 'checkpoint selection protocol awaits user confirmation')
        require(int(os.environ.get('WORLD_SIZE', '1')) == 4, 'recipe fixes 4 ranks, global batch32')
        output = Path(self.output_dir)
        if resume is not None:
            checkpoint = Path(resume).resolve()
            require(checkpoint.parent == output, 'resume checkpoint must belong to this run')
            require(json.loads((checkpoint / 'f1_run_binding.json').read_text()) == self.binding, 'resume recipe/release/code/base mismatch')
            for name in ('trainer_state.json', 'optimizer.pt', 'scheduler.pt'):
                require((checkpoint / name).is_file(), f'incomplete resume: {name}')
            for rank in range(4):
                require((checkpoint / f'rng_state_{rank}.pth').is_file(), f'missing rank{rank} RNG state')
        else:
            # Initialize the existing DDP runtime before any rank can create output.
            # Only rank0 checks the shared directory, then every rank receives it.
            from accelerate import PartialState
            import torch
            distributed = PartialState()
            error = None
            if distributed.is_main_process and output.exists() and any(output.iterdir()):
                error = 'new F1 run requires an empty independent output directory'
            if torch.distributed.is_initialized():
                errors = [error]
                torch.distributed.broadcast_object_list(errors, src=0)
                error = errors[0]
            require(error is None, error)
        from .monitoring import configure_wandb, evaluation_scalars
        self.wandb_run_id = configure_wandb(self.output_dir, resume=resume is not None)
        self.configure_shared()
        import torch
        from transformers import AutoProcessor, TrainerCallback, set_seed
        from opendm.model.dm05.dm05_lora import unwrap_dm05_model
        from .evaluation import evaluate_model
        set_seed(self.trainer_config.seed)
        self.model = self.model_config.build_model(use_lora=True)
        self.optimizer_config.prepare_model(self.model)
        base = unwrap_dm05_model(self.model)
        base.config.use_cache = False
        base.model.vlm.config.use_cache = False
        self.processor = AutoProcessor.from_pretrained(self.base_model, local_files_only=True)
        trainer = self.build_trainer(self.model, self.processor)
        from .distributed_validation import create_validation_group, run_rank_zero
        from .validation_artifacts import checkpoint_identity
        validation_group = create_validation_group()
        exp = self

        class GenerationValidation(TrainerCallback):
            def on_train_begin(self, args, state, control, **kwargs):
                error = None
                if state.is_world_process_zero:
                    try:
                        import wandb
                        require(wandb.run is not None, 'W&B run was not initialized')
                        require(wandb.run.id == exp.wandb_run_id, 'W&B run identity mismatch')
                        wandb.config.update({'f1_recipe': exp.recipe, 'f1_binding': exp.binding})
                        (Path(args.output_dir) / 'wandb_run.json').write_text(json.dumps(
                            {'id': wandb.run.id, 'project': wandb.run.project, 'url': wandb.run.url}, indent=2))
                    except Exception as exc:
                        error = f'{type(exc).__name__}: {exc}'
                if torch.distributed.is_initialized():
                    errors = [error]
                    torch.distributed.broadcast_object_list(errors, src=0)
                    error = errors[0]
                if error:
                    raise RuntimeError('F1 W&B initialization failed: ' + error)
                return control

            def on_save(self, args, state, control, **kwargs):
                # All ranks wait; only rank0 runs unwrapped generation, never DDP forward.
                trainer.accelerator.wait_for_everyone()
                def evaluate_saved():
                    report = evaluate_model(trainer.accelerator.unwrap_model(trainer.model), exp.release, exp.processor,
                                   Path(args.output_dir) / f'validation-{state.global_step}',
                                   device=args.device, autocast=trainer.accelerator.autocast,
                                   checkpoint_identity=checkpoint_identity(Path(args.output_dir) / f'checkpoint-{state.global_step}'))
                    return evaluation_scalars(report)
                metrics = run_rank_zero(evaluate_saved, validation_group)
                trainer.log(metrics)
                return control

        trainer.add_callback(GenerationValidation())
        trainer.train(resume_from_checkpoint=str(resume) if resume else None)
        trainer.save_state()
        # Step checkpoints retain optimizer/scheduler/RNG and validation provenance.
        return trainer
