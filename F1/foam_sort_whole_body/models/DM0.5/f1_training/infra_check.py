"""Synthetic CPU checkpoint/resume and multi-process validation checks; no policy training."""
import argparse
import json
import os
from pathlib import Path
import tempfile


def distributed_check(output):
    import time
    from datetime import timedelta
    import torch.distributed as dist
    from .distributed_validation import create_validation_group, run_rank_zero
    # A deliberately short training-group timeout reproduces the waiting risk.
    dist.init_process_group('gloo', timeout=timedelta(seconds=2))
    group=create_validation_group()
    def slow():
        time.sleep(3)
        return {'metric':17}
    assert run_rank_zero(slow,group)=={'metric':17}
    try:
        run_rank_zero(lambda: (_ for _ in ()).throw(ValueError('fixture failure')),group)
    except RuntimeError as exc:
        assert 'fixture failure' in str(exc)
    else:
        raise AssertionError('rank-zero error was not propagated')
    # Verify the original training group is still usable after the long evaluation.
    import torch
    value=torch.tensor(1.)
    dist.all_reduce(value)
    assert value.item()==dist.get_world_size()
    if dist.get_rank()==0:
        from .validation_artifacts import write_new
        write_new(output,{'status':'passed','world_size':dist.get_world_size(),'backend':'CPU gloo',
                          'validation_exceeds_training_timeout':True,'errors_propagated_to_all_ranks':True,
                          'training_group_usable_after_validation':True,'gpu_used':False,'policy_training':False})
    dist.destroy_process_group(group)
    dist.destroy_process_group()


def resume_check(args):
    os.environ['WORLD_SIZE']='1'
    import torch
    import numpy as np
    from transformers import TrainerCallback, set_seed
    from .experiment import F1Exp
    from .trainer import F1Trainer
    from .validation_artifacts import write_new
    torch.set_num_threads(2)
    class Data:
        def __len__(self): return 128
        def balanced_indices(self,count,*,seed,epoch):
            return np.random.default_rng(seed+epoch).integers(0,len(self),size=count)
        def __getitem__(self,key):
            index,epoch,draw=key
            return {'action':torch.tensor([(index+epoch+draw)/128.])}
    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__(); self.weight=torch.nn.Parameter(torch.tensor([.3]))
        def forward(self,action,**kwargs):
            loss=((self.weight-action+torch.rand_like(action)*.01)**2).mean()
            return {'loss':loss,'fm_loss':loss.detach()}
    class Stop(TrainerCallback):
        def __init__(self,step): self.step=step
        def on_step_end(self,args,state,control,**kwargs):
            if state.global_step>=self.step: control.should_training_stop=True
            return control
    def make(directory,stop):
        set_seed(42)
        exp=F1Exp(args.release,args.base_model,directory,engineering=True).configure_shared()
        cfg=exp.trainer_config
        cfg.bf16=False; cfg.tf32=False; cfg.save_steps=2
        cfg.dataloader_num_workers=0; cfg.dataloader_persistent_workers=False; cfg.dataloader_prefetch_factor=None
        trainer=F1Trainer(model=Model(), exp_config=exp, train_dataset=Data(),
                          data_collator=lambda batch:{'action':torch.stack([x['action'] for x in batch])})
        trainer.add_callback(Stop(stop))
        return trainer
    with tempfile.TemporaryDirectory(prefix='f1-infra-cpu-') as temporary:
        root=Path(temporary)
        full=make(root/'full',4); full.train()
        full_weight=full.model.weight.detach().clone()
        full_optimizer=full.optimizer.state_dict(); full_scheduler=full.lr_scheduler.state_dict()
        interrupted=make(root/'resume',2); interrupted.train()
        checkpoint=root/'resume/checkpoint-2'
        for name in ('optimizer.pt','scheduler.pt','rng_state.pth','trainer_state.json','norm_stats.json','f1_run_binding.json','f1_release.json','f1_experiment.yaml'):
            assert (checkpoint/name).is_file(),name
        resumed=make(root/'resume',4); resumed.train(resume_from_checkpoint=str(checkpoint))
        assert torch.equal(full_weight,resumed.model.weight.detach())
        assert full_scheduler==resumed.lr_scheduler.state_dict()
        for key,value in full_optimizer['state'][0].items():
            actual=resumed.optimizer.state_dict()['state'][0][key]
            assert torch.equal(value,actual) if torch.is_tensor(value) else value==actual
        import wandb
        run=wandb.init(project='F1_foam_DM05_infra',mode='offline',dir=str(root),settings=wandb.Settings(console='off'))
        run.log({'eval_fixture':1.25,'train/global_step':5000})
        run.finish()
        assert list(root.glob('wandb/offline-run-*/*.wandb'))
        result={'status':'passed','scope':'actual F1Trainer with a one-parameter synthetic CPU model',
                'policy_training':False,'dm05_weights_loaded':False,'gpu_used':False,
                'synthetic_optimizer_steps':8,'schedule_max_steps':full.args.max_steps,
                'checkpoint_sidecars_optimizer_scheduler_rng_saved':True,
                'interrupted_resume_matches_uninterrupted_weights_optimizer_scheduler':True,
                'wandb_offline_write':True,'wandb_online_upload_tested':False}
    write_new(args.output,result)
    print(json.dumps(result,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distributed',action='store_true')
    parser.add_argument('--release')
    parser.add_argument('--base-model')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.distributed: distributed_check(args.output)
    else:
        parser.error('resume check needs --release and --base-model') if not(args.release and args.base_model) else None
        resume_check(args)


if __name__=='__main__': main()
