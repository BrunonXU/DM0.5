"""Offline trajectory plots and an explicit primary/fallback decision record."""
import csv
import json
from pathlib import Path
import numpy as np
from f1_data.reader import require, sha256
from .validation_artifacts import load_report, write_new


def build_review(report_path, output, *, limit=12):
    require(limit > 0, 'positive review window limit required')
    report_path = Path(report_path).resolve()
    report = load_report(report_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fields = [('left_position_mm','Left EEF position error (mm)'),
              ('right_position_mm','Right EEF position error (mm)')]
    # Rank per-window arm errors only to make a review queue, not a checkpoint score.
    rows = []
    for index, sample in enumerate(report['selection']['samples']):
        records = [r for r in report['records'] if r['episode']==sample['episode'] and r['start']==sample['start']]
        require(records, 'missing window records')
        score = max((r['metrics']['20'].get(k) if r['metrics']['20'].get(k) is not None else float('inf'))
                    for r in records for k,_ in fields)
        rows.append((index, sample, records, score))
    rows.sort(key=lambda x: (not any(not r['ik']['success'] for r in x[2]), -x[3], x[0]))
    csv_rows = []
    for index, sample, records, score in rows:
        for r in records:
            m = r['metrics']['20']
            csv_rows.append({'episode':sample['episode'], 'start':sample['start'], 'seed':r['seed'],
                             'ik_success':r['ik']['success'], 'ik_reason':r['ik'].get('reason',''),
                             **{k:v for k,v in m.items() if isinstance(v,(int,float)) or v is None}})
    with (output/'windows.csv').open('x') as stream:
        # Moving/stopped subsets can be absent in different windows.
        keys=list(dict.fromkeys(key for row in csv_rows for key in row))
        writer=csv.DictWriter(stream, fieldnames=keys); writer.writeheader(); writer.writerows(csv_rows)
    images = []
    from .validation_metrics import rotation_errors
    for index, sample, records, _ in rows[:limit]:
        fig, axes = plt.subplots(5, 1, figsize=(12,15), sharex=True)
        t=np.arange(50)/30
        for number, r in enumerate(records):
            with np.load(report_path.parent/f'{index:06d}_seed{r["seed"]}.npz') as chunk:
                p=chunk['physical_prediction']; truth=chunk['physical_target']; state=chunk['state19']
            label=f'seed {r["seed"]}'
            for side,offset,color in [('L',0,'tab:blue'),('R',9,'tab:orange')]:
                axes[0].plot(t,np.linalg.norm(p[:,offset:offset+3]-truth[:,offset:offset+3],axis=-1)*1000,
                             color=color,alpha=.55,label=f'{side} {label}')
                axes[1].plot(t,rotation_errors(p[:,offset+3:offset+9],truth[:,offset+3:offset+9]),color=color,alpha=.55,label=f'{side} {label}')
            for dim,color,name in [(18,'tab:blue','left'),(19,'tab:orange','right')]:
                axes[2].plot(t,p[:,dim],color=color,alpha=.55,label=f'{name} {label}')
                if number==0: axes[2].plot(t,truth[:,dim],color=color,linestyle='--',label=f'{name} target')
            for dim,color,name,axis in [(20,'tab:blue','vx',3),(21,'tab:orange','vy',3),(22,'tab:green','wz',4)]:
                axes[axis].plot(t,p[:,dim],color=color,alpha=.55,label=f'{name} {label}')
                if number==0:
                    axes[axis].plot(t,truth[:,dim],color=color,linestyle='--',label=f'{name} target (reconstructed)')
                    axes[axis].axhline(state[dim-4],color=color,linestyle=':',alpha=.5,label=f'{name} current input')
        for ax,title in zip(axes,['EEF position error (mm)','EEF rotation error (deg)','Grippers (0-100)','Base velocity (m/s)','Base yaw rate (rad/s)']):
            ax.set_ylabel(title); ax.axvspan(0,20/30,color='grey',alpha=.08); ax.grid(alpha=.2)
            ax.legend(fontsize=6,loc='upper right',ncol=3)
        axes[-1].set_xlabel('Seconds from observation anchor; shaded: execute first 20 steps')
        fig.suptitle(f'{sample["episode"]}, start={sample["start"]}; offline label comparison')
        fig.tight_layout()
        name=f'window_{index:06d}.svg'; fig.savefig(output/name); plt.close(fig)
        images.append((sample,name))
    lines=['# Validation trajectory review','',f'Source: `{report_path}`',
           f'Report SHA256: `{sha256(report_path)}`','',
           'Engineering aid only. Base targets are reconstructed; these plots do not prove real stopping or closed-loop success.',
           'Review queue: IK-rejected windows first, then largest arm position error. This is not the checkpoint ranking.',
           f'Showing {len(images)}/{len(rows)} windows; use --limit to expand. All scalar rows: [windows.csv](windows.csv).','']
    for sample,name in images:
        lines += [f'## {sample["episode"]}, start {sample["start"]}', '',f'![trajectory]({name})','']
    (output/'README.md').write_text('\n'.join(lines))
    write_new(output/'review_manifest.json', {'report':str(report_path),'report_sha256':sha256(report_path),
                'plotted_windows':len(images),'total_windows':len(rows),'automatic_task_acceptance':False})


def record_decision(comparison_path, review_path, output):
    comparison_path, review_path = Path(comparison_path).resolve(), Path(review_path).resolve()
    comparison=json.loads(comparison_path.read_text()); review=json.loads(review_path.read_text())
    require(comparison.get('schema')=='f1_checkpoint_comparison_v1', 'invalid comparison schema')
    reports={}
    for path,digest in comparison['report_files'].items():
        require(sha256(path)==digest, 'comparison source report changed')
        reports[path]=load_report(path)
    require(all(r['selection'].get('stage')=='pass_b' for r in reports.values()), 'final selection requires Pass B')
    from .experiment import read_recipe
    bounds=read_recipe()['evaluation']['pass_b']['candidate_count']
    require(bounds[0]<=len(reports)<=bounds[1], 'Pass B requires 2-3 frozen candidates')
    chosen={key:str(Path(review[key]).resolve()) for key in ('primary','fallback')}
    require(chosen['primary']!=chosen['fallback'] and all(p in reports for p in chosen.values()), 'invalid primary/fallback')
    for key in ('selection_reason','base_coordination_review','ik_and_invalid_output_review'):
        require(isinstance(review.get(key),str) and review[key].strip(), f'missing human review: {key}')
    payload={'schema':'f1_checkpoint_decision_v1','automatic_selection':False,'deployment_performed':False,
             'comparison':str(comparison_path),'comparison_sha256':sha256(comparison_path),
             'human_review':review,'human_review_sha256':sha256(review_path),
             'selected':{key:reports[path]['checkpoint_identity'] for key,path in chosen.items()}}
    write_new(output,payload)
