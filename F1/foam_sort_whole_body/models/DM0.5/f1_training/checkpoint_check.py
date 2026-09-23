"""Fresh, untrained DM0.5 LoRA save -> independent evaluator load; no optimizer."""
import argparse
import gc
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',required=True)
    parser.add_argument('--base-model',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve(); output.mkdir(parents=True,exist_ok=False)
    os.environ['WORLD_SIZE']='1'
    import torch
    from transformers import set_seed
    from .experiment import F1Exp
    from .validation_protocol import build_selection
    from .validation_artifacts import write_new, load_report
    from f1_data.reader import sha256
    set_seed(42); torch.set_num_threads(4)
    with tempfile.TemporaryDirectory(prefix='f1-untrained-checkpoint-') as temporary:
        root=Path(temporary)
        exp=F1Exp(args.release,args.base_model,root/'run',engineering=True).configure_shared()
        print('Building fresh untrained LoRA; no optimizer or training',flush=True)
        model=exp.model_config.build_model(use_lora=True).to('cuda')
        checkpoint=root/'checkpoint-0'; model.save_pretrained(checkpoint)
        shutil.copyfile(exp.release.stats_path,checkpoint/'norm_stats.json')
        write_new(checkpoint/'f1_run_binding.json',exp.binding)
        del model; gc.collect(); torch.cuda.empty_cache()
        selection=build_selection(exp.release,windows_per_episode=1,generation_seeds=[42],
                                  flow_windows_per_episode=1,flow_seeds=[43])
        write_new(root/'selection.json',{'release_sha256':sha256(exp.release.path),'selection':selection})
        print('Reloading saved adapter through standalone evaluator on two engineering validation episodes',flush=True)
        command=[sys.executable,'-m','f1_training.validation','evaluate','--release',str(exp.release.path),
                 '--checkpoint',str(checkpoint),'--selection',str(root/'selection.json'),
                 '--output',str(output/'evaluation'),'--device','cuda:0','--engineering']
        with (output/'evaluator.log').open('x') as stream:
            subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
        report=load_report(output/'evaluation/report.json')
        assert len(report['records'])==2 and len(report['flow_records'])==2
        assert all(r['metrics']['50']['nonfinite_elements']==0 for r in report['records'])
        result={'status':'passed','policy_training':False,'optimizer_steps':0,
                'fresh_adapter_saved_and_reloaded':True,'checkpoint_weights_discarded_after_check':True,
                'independent_evaluator_full_dm05':True,'validation_episodes':2,
                'generated_chunks':2,'held_out_fm_calls':2,'generated_finite':True,
                'ik_rejected_prefixes':sum(not r['ik']['success'] for r in report['records']),
                'interpretation':'Untrained outputs only test infrastructure, not policy quality',
                'binding':exp.binding}
    write_new(output/'check.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='binding'},indent=2))


if __name__=='__main__': main()
