#!/usr/bin/env python3
"""Diagnose fixed RLT checkpoints: action ranking, gradients and prefix support.

No optimizer steps, label changes, deployment or robot communication. Modified
actions are critic probes, never labelled as known task failures/successes.
"""
import argparse
import json
from pathlib import Path
import sys

TASK=Path(__file__).resolve().parents[1];sys.path.insert(0,str(TASK/'src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--comparison',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    import torch
    import torch.nn.functional as F
    torch.set_num_threads(2)
    from f1_toys.rlt_online import LocalLearner,sha,atomic_json
    from f1_toys.rlt_demo_seed import load_demo_seed
    from f1_toys.rlt_critic_calibration import concatenate,subset,load_terminals
    from f1_toys.rlt_stage2 import bc_targets,actor_parameters
    from f1_toys.rlt_evaluation import orientation_metrics
    cfg=json.loads((args.data/'candidate-data-config.json').read_text())['f1']
    migration=json.loads((args.data/'migration.json').read_text())
    parts={s:[] for s in ('train','validation')};ids={s:[] for s in parts}
    for entry in migration['sources']:
        root=Path(entry['cache']);manifest=json.loads((root/'manifest.json').read_text())
        for name,value in manifest['files_sha256'].items():assert sha(root/name)==value
        part=torch.load(root/'replay.pt',weights_only=True)
        parts[entry['split']].append(part);ids[entry['split']]+=[entry['episode']]*len(part['actions'])
    demos,records,_=load_demo_seed(cfg['demo_seed'],cfg['replay']['feature_identity'])
    terms,_=load_terminals(cfg['critic_terminal_cache'],cfg['replay']['feature_identity'])
    local={s:concatenate(v) for s,v in parts.items()}
    data={s:concatenate([local[s],demos[s]]) for s in parts}
    ids['validation'] += [r['source_run'] for r in records['validation']]
    def stats(value):
        value=value.detach().float().reshape(-1)
        if not len(value):return dict(count=0)
        return dict(count=len(value),mean=float(value.mean()),min=float(value.min()),
                    median=float(value.median()),p95=float(torch.quantile(value,.95)),max=float(value.max()))
    def coverage(part):
        mask=part['action_time_mask'];human=(part['intervene_flags']&mask).any(-1)
        full=(part['execution_fraction']==1).all(-1)
        return {label:dict(rows=int(which.sum()),steps=stats(mask[which].sum(-1)),
            full_mask=int(mask[which].all(-1).sum()),full_consumed=int(full[which].sum()),
            success_terminal=int((which&part['terminated']&(part['reward']==1)).sum()),
            failure_terminal=int((which&part['terminated']&(part['reward']==0)).sum()))
            for label,which in [('human',human),('autonomous',~human)]}
    report=dict(parameter_updates=0,robot_commands=0,deployed=False,source_sha256=sha(__file__),
                data_migration_sha256=sha(args.data/'migration.json'),
                coverage={s:coverage(part) for s,part in data.items()},checkpoints=[])
    args.output.mkdir(parents=True,exist_ok=False)
    initial=LocalLearner(cfg['core_root'],cfg['training'],cfg['initial_head'])
    with torch.no_grad():base={s:initial.model.sac_forward(v['obs'],deterministic=True)[0].reshape_as(v['actions']) for s,v in data.items()}
    def canonical(actions):
        first=F.normalize(actions[...,3:6],dim=-1)
        second=actions[...,6:9]-(first*actions[...,6:9]).sum(-1,keepdim=True)*first
        second=F.normalize(second,dim=-1)
        return torch.stack((first,second,torch.cross(first,second,dim=-1)),-2)
    def turned(actions,axis,degrees):
        import math
        out=actions.clone();rot=canonical(out);angle=math.radians(degrees)
        r=torch.eye(3);i,j=[v for v in range(3) if v!=axis]
        r[i,i]=r[j,j]=math.cos(angle);r[i,j]=-math.sin(angle);r[j,i]=math.sin(angle)
        out[...,3:9]=(r@rot)[...,:2,:].reshape(*out.shape[:-1],6)
        return out
    for variant in ('decision-bc','decision-q'):
        steps=(1024,) if variant=='decision-bc' else (128,256,512,1024)
        for step in steps:
            path=args.comparison/variant if step==1024 else args.comparison/variant/('step-'+str(step))
            saved=torch.load(path/'learner.pt',weights_only=True)
            learner=LocalLearner(cfg['core_root'],saved['identity']['recipe'],cfg['initial_head'])
            learner.restore(path,saved['identity'])
            item=dict(variant=variant,step=step,checkpoint_sha256=sha(path/'learner.pt'),splits={})
            for split,part in data.items():
                state=learner.model._critic_state(part['obs']).detach();mask=part['action_time_mask'];human=mask&part['intervene_flags']
                elapsed=part['elapsed_steps'];full=torch.ones_like(mask)
                with torch.no_grad():
                    pi=learner.model.sac_forward(part['obs'],deterministic=True)[0].reshape_as(part['actions'])
                def q(actions,observed=False):
                    return learner.critic(state,actions,mask if observed else full,elapsed,
                        part['execution_fraction'] if observed else None)
                with torch.no_grad():
                    qpi=q(pi);qb=q(base[split]);qd=q(part['actions'],True)
                    fixed=pi.clone();fixed[...,3:9]=base[split][...,3:9];qfix=q(fixed)
                    canonical_pi=pi.clone();canonical_pi[...,3:9]=canonical(pi)[...,:2,:].reshape(*pi.shape[:-1],6)
                    canonical_fixed=pi.clone();canonical_fixed[...,3:9]=canonical(base[split])[...,:2,:].reshape(*pi.shape[:-1],6)
                    canonical_gain=q(canonical_pi)-q(canonical_fixed)
                    humanfixed=pi.clone();humanfixed[...,3:9]=torch.where(human[...,None],part['actions'][...,3:9],pi[...,3:9]);qh=q(humanfixed)
                    gauge=pi.clone();gauge[...,3:9]*=.5
                    angles=orientation_metrics(pi,base[split],full)
                    probes=[]
                    for axis in range(3):
                        for degrees in (-10,-5,5,10):
                            baseline=turned(base[split],axis,0);alt=turned(base[split],axis,degrees)
                            delta=q(alt)-q(baseline)
                            probes.append(dict(axis=axis,degrees=degrees,q1_gain=stats(delta[:,0]),
                                head1_prefers_fraction=float((delta[:,0]>0).float().mean()),
                                min_twin_gain=stats(q(alt).min(-1).values-q(baseline).min(-1).values)))
                variable=pi.detach().requires_grad_(True)
                labels,_=bc_targets(learner.model,part['obs'],part['actions'],part['intervene_flags'])
                bc=cfg['training']['bc_weight']*((variable-labels).square().mean(-1)*mask).sum()/mask.sum()
                qloss=-cfg['training']['q_weight']*q(variable)[:,0].mean()
                bg,=torch.autograd.grad(bc,variable,retain_graph=True)
                qg,=torch.autograd.grad(qloss,variable)
                groupgrad={}
                for name,cols in [('xyz',slice(0,3)),('rotation',slice(3,9)),('gripper',slice(9,10))]:
                    b,g=bg[...,cols],qg[...,cols]
                    groupgrad[name]=dict(bc_norm=float(b.norm()),q_norm=float(g.norm()),
                        ratio_q_to_bc=float(g.norm()/b.norm().clamp_min(1e-20)),
                        q_squared_gradient_on_unexecuted_fraction=float((g.square()*(~mask)[...,None]).sum()/g.square().sum().clamp_min(1e-20)))
                # Small action-space Q-ascent probe: diagnostic, not an update
                # to the network or evidence of a real transition outcome.
                normalized=-qg/qg.norm(dim=-1,keepdim=True).clamp_min(1e-20)
                small=pi+.01*normalized
                with torch.no_grad():smallq=q(small)
                s=dict(rows=len(mask),q1_actual_records=stats(qd[:,0]),q1_actor_full=stats(qpi[:,0]),
                    q1_baseline_full=stats(qb[:,0]),q1_actor_above_one=float((qpi[:,0]>1).float().mean()),
                    actor_vs_baseline_q1_gain=stats(qpi[:,0]-qb[:,0]),
                    actor_rotation_vs_bc_rotation_q1_gain=stats(qpi[:,0]-qfix[:,0]),
                    actor_rotation_preferred_fraction=float((qpi[:,0]>qfix[:,0]).float().mean()),
                    canonical_rotation_q1_gain=stats(canonical_gain[:,0]),
                    canonical_rotation_q1_preferred_fraction=float((canonical_gain[:,0]>0).float().mean()),
                    canonical_rotation_q2_gain=stats(canonical_gain[:,1]),
                    actor_vs_human_rotation_q1_gain=stats(qpi[:,0]-qh[:,0]),
                    rotation_vs_bc=angles,gradient=groupgrad,rotation_probes=probes,
                    small_q_ascent_gain=stats(smallq[:,0]-qpi[:,0]),
                    human_rotation_error_before=orientation_metrics(pi,part['actions'],human),
                    human_rotation_error_after_small_q_ascent=orientation_metrics(small,part['actions'],human))
                # qg above is now a gradient; reevaluate the unchanged physical
                # action's gauge explicitly rather than reusing that variable.
                with torch.no_grad():s['physical_equivalent_halfscale_q1_change']=stats(q(gauge)[:,0]-qpi[:,0])
                if split=='train':
                    # Reproduce one actual stochastic/dropout training batch's
                    # weighted parameter gradients, without optimizer.step.
                    torch.manual_seed(20261102)
                    batch=concatenate([subset(v,torch.randint(len(v['actions']),(32,))) for v in (local['train'],demos['train'])])
                    pred=learner.model.sac_forward(batch['obs'],deterministic=False,apply_reference_dropout=True,reference_dropout_prob=.5)[0]
                    lbl,_=bc_targets(learner.model,batch['obs'],batch['actions'],batch['intervene_flags'])
                    m=batch['action_time_mask'];params=actor_parameters(learner.model)
                    lbc=5*((pred.reshape_as(lbl)-lbl).square().mean(-1)*m).sum()/m.sum()
                    lq=-.1*learner.critic(learner.model._critic_state(batch['obs']),pred,torch.ones_like(m),batch['elapsed_steps'])[:,0].mean()
                    gb=torch.autograd.grad(lbc,params,retain_graph=True);gq=torch.autograd.grad(lq,params)
                    bn=sum(g.square().sum() for g in gb).sqrt();qn=sum(g.square().sum() for g in gq).sqrt()
                    s['training_parameter_gradients']=dict(bc_norm=float(bn),q_norm=float(qn),ratio=float(qn/bn.clamp_min(1e-20)),
                        cosine=float(sum((b*g).sum() for b,g in zip(gb,gq))/(bn*qn).clamp_min(1e-20)))
                    if step==1024 and variant=='decision-q':
                        batches=[]
                        for seed in range(20261102,20261118):
                            torch.manual_seed(seed)
                            batch=concatenate([subset(v,torch.randint(len(v['actions']),(32,))) for v in (local['train'],demos['train'])])
                            pred=learner.model.sac_forward(batch['obs'],deterministic=False,apply_reference_dropout=True,reference_dropout_prob=.5)[0]
                            lbl,_=bc_targets(learner.model,batch['obs'],batch['actions'],batch['intervene_flags'])
                            m=batch['action_time_mask'];error=(pred.reshape_as(lbl)-lbl).square().mean(-1)
                            losses=[5*(error*m).sum()/m.sum(),5*error.mean(),
                                -.1*learner.critic(learner.model._critic_state(batch['obs']),pred,torch.ones_like(m),batch['elapsed_steps'])[:,0].mean()]
                            grads=[torch.autograd.grad(loss,params,retain_graph=i<2) for i,loss in enumerate(losses)]
                            norms=[sum(g.square().sum() for g in group).sqrt() for group in grads]
                            batches.append(dict(seed=seed,current_bc_norm=float(norms[0]),full_teacher_bc_norm=float(norms[1]),q_norm=float(norms[2]),
                                current_q_to_bc=float(norms[2]/norms[0]),full_teacher_q_to_bc=float(norms[2]/norms[1]),
                                current_cosine=float(sum((b*g).sum() for b,g in zip(grads[0],grads[2]))/(norms[0]*norms[2])),
                                full_teacher_cosine=float(sum((b*g).sum() for b,g in zip(grads[1],grads[2]))/(norms[1]*norms[2]))))
                        s['parameter_gradient_batches']=batches
                        s['parameter_gradient_summary']={key:stats(torch.tensor([r[key] for r in batches])) for key in batches[0] if key!='seed'}
                item['splits'][split]=s
                print(json.dumps(dict(variant=variant,step=step,split=split,q1=stats(qpi[:,0]),rotation_preference=s['actor_rotation_vs_bc_rotation_q1_gain'],gradient=groupgrad)),flush=True)
            report['checkpoints'].append(item);atomic_json(args.output/'report.json',report)


if __name__=='__main__':main()
