#!/usr/bin/env python3
"""Recover real TensorBoard scalars into W&B and follow an already running job."""
import argparse
import json
from pathlib import Path
import time

from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
import wandb


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--id',required=True)
    p.add_argument('--target-step',type=int,required=True)
    p.add_argument('--mode',choices=['online','offline'],default='offline')
    a=p.parse_args()
    out=a.run/'wandb-recovery';out.mkdir(exist_ok=True)
    log=EventAccumulator(str(a.run/'tensorboard'),size_guidance={'scalars':0})
    run=wandb.init(project='f1_complexytoys',name=a.run.name,id=a.id,mode=a.mode,dir=str(out),
        config=dict(source='TensorBoard scalar backfill + live tail',original_run=str(a.run),
                    original_training_started_without_wandb=True,recipe='sft_eef_lora',
                    total_steps=a.target_step,global_batch=8,micro_batch=2,
                    dataset='/u01/user/datasets/f1_toys_v1_eef_20261016/dataset',
                    roi='A [.24,.04,.96,1]',action='right EEF10',augmentation=True),
        settings=wandb.Settings(x_disable_stats=True))
    # Disable this bridge process's system metrics: they aren't training metrics.
    last=-1;last_progress=time.monotonic()
    try:
        while True:
            log.Reload();rows={}
            for key in log.Tags()['scalars']:
                for event in log.Scalars(key):
                    if event.step>last:
                        rows.setdefault(event.step,dict(source_wall_time=event.wall_time))[key]=event.value
            for step,values in sorted(rows.items()):
                values['optimizer_step']=step
                run.log(values,step=step)
                last=step;last_progress=time.monotonic()
            (out/'status.json').write_text(json.dumps(dict(run_id=run.id,url=run.url if a.mode=='online' else None,
                mode=a.mode,last_tensorboard_step=last,target_step=a.target_step,source=str(a.run/'tensorboard'),
                complete=last>=a.target_step-1,updated_unix=time.time()),indent=2)+'\n')
            print(json.dumps(dict(mode=a.mode,last_step=last)),flush=True)
            if last>=a.target_step-1:break
            if time.monotonic()-last_progress>900:raise TimeoutError('No new training metrics for 15 minutes')
            time.sleep(5)
        run.summary['recovered_through_step']=last
        run.summary['system_metrics_not_recovered']=True
        manifest=a.run/'lora_manifest.json'
        if manifest.exists():
            config=json.loads(manifest.read_text())
            run.summary['trainable_parameters']=config['trainable_parameters']
            run.summary['lora_modules']=len(config['modules'])
    finally:run.finish()


if __name__=='__main__':main()
