"""Task-only Trainer extension. Network and flow matching remain shared."""
import json
from pathlib import Path
import shutil
from opendm.trainer.trainer import DMTrainer
from .data import TrainingDrawSampler


class F1Trainer(DMTrainer):
    def _get_train_sampler(self, train_dataset=None):
        dataset = self.train_dataset if train_dataset is None else train_dataset
        return TrainingDrawSampler(dataset, seed=self.args.seed)

    def _save_checkpoint(self, model, trial, metrics=None):
        super()._save_checkpoint(model, trial, metrics)
        if self.args.should_save:
            target = Path(self.args.output_dir) / f'checkpoint-{self.state.global_step}'
            shutil.copyfile(self.exp_config.release.path, target / 'f1_release.json')
            shutil.copyfile(self.exp_config.recipe_path, target / 'f1_experiment.yaml')
            (target / 'f1_run_binding.json').write_text(json.dumps(self.exp_config.binding, indent=2))
