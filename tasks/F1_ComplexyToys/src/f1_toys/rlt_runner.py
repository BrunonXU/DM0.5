"""Small task hooks on the unchanged official asynchronous runner."""
from rlinf.runners.async_embodied_runner import AsyncEmbodiedRunner


class F1AsyncRunner(AsyncEmbodiedRunner):
    def init_workers(self):
        super().init_workers()
        spec = self.cfg.f1.get('bootstrap_checkpoint')
        if (not self.cfg.runner.get('resume_dir') and spec
                and spec.get('schema') == 'f1_rlt_offline_bootstrap_v1'):
            states = self.actor.training_state().wait()
            if not states or any(s['step'] != spec['parent_step'] for s in states):
                raise ValueError('Runner/offline bootstrap step mismatch')
            self.global_step = int(spec['parent_step'])

    def print_metrics_table_async(self, *args, **kwargs):
        result = super().print_metrics_table_async(*args, **kwargs)
        # Official runner emits several metric dictionaries for one step.
        # Commit only after all of them, including ranked metrics, were logged.
        wandb = self.metric_logger.logger.get('wandb')
        if wandb is not None:
            wandb.log({}, step=self.global_step, commit=True)
        return result
