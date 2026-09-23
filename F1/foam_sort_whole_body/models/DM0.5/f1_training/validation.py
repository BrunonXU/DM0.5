"""Standalone F1 ports of fixed manifests, checkpoint validation and paired sweep."""
import argparse
import itertools
import json
import subprocess
import sys
from pathlib import Path
from f1_data.reader import require, sha256
from .data import Release
from .validation_protocol import build_selection, build_recipe_selection, paired_comparison, macro_summary, rank_candidates
from .validation_artifacts import write_new, load_report, validate_checkpoint


def compare_reports(paths, *, repeats, seed):
    reports = [(str(Path(p).resolve()), load_report(p)) for p in paths]
    require(len(reports) >= 2, 'at least two reports required')
    # Never silently rank measurements from different samples or implementation.
    contract = reports[0][1].get('comparison_contract')
    require(contract and all(r.get('comparison_contract') == contract for _,r in reports), 'comparison contracts differ')
    require(len({name for name, _ in reports}) == len(reports), 'duplicate report paths')
    identities = [r['checkpoint_identity'] for _,r in reports]
    require(all(i['binding'] == identities[0]['binding'] for i in identities), 'checkpoint run bindings differ')
    require(len({i['checkpoint'] for i in identities}) == len(identities), 'duplicate checkpoints')
    from .experiment import read_recipe
    spec = read_recipe()['evaluation']
    require(spec['checkpoint_weighting_status'] == 'user_confirmed_20260917', 'checkpoint weights not confirmed')
    return {'schema':'f1_checkpoint_comparison_v1', 'automatic_selection':False,
            'report_files':{name:sha256(name) for name,_ in reports},
            'ranking':rank_candidates(dict(reports), weights=spec['checkpoint_ranking']['group_weights']),
            'summaries':{name:macro_summary(report) for name,report in reports},
            'comparisons':[{'left':a, 'right':b, **paired_comparison(ra,rb,repeats=repeats,seed=seed)}
                           for (a,ra),(b,rb) in itertools.combinations(reports,2)]}


def run_sweep(args):
    """One process per checkpoint releases GPU memory; completed attempts are reused."""
    release = Release(args.release, engineering=args.engineering)
    selection = json.loads(Path(args.selection).read_text())
    require(selection['release_sha256'] == sha256(release.path), 'selection/release mismatch')
    from .validation_protocol import digest_document
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    checkpoints = [str(Path(p).resolve()) for p in args.checkpoints]
    require(len(checkpoints) == len(set(checkpoints)), 'duplicate checkpoints')
    identities = [validate_checkpoint(p, release) for p in checkpoints]
    plan = {'checkpoints':identities, 'selection_sha256':digest_document(selection['selection']),
            'release':str(release.path), 'device':args.device, 'engineering':args.engineering}
    plan_path = root/'sweep_plan.json'
    if plan_path.exists():
        require(json.loads(plan_path.read_text()) == plan, 'sweep plan changed; use a new output directory')
    else:
        write_new(plan_path, plan)
    reports = []
    for index, (checkpoint, identity) in enumerate(zip(checkpoints, identities)):
        target = root/f'{index:03d}_{Path(checkpoint).name}'
        attempts = sorted(p for p in target.glob('attempt-*') if p.is_dir())
        completed = []
        for attempt in attempts:
            if not (attempt/'report.json').exists():
                continue
            report = load_report(attempt/'report.json')
            require(report['checkpoint_identity'] == identity, 'sweep checkpoint changed')
            require(report['comparison_contract']['selection_sha256'] == plan['selection_sha256'], 'sweep selection changed')
            completed.append(attempt/'report.json')
        if completed:
            reports.append(str(completed[-1]))
            continue
        used = {p.stem for p in target.glob('attempt-*.log')} | {p.name for p in attempts}
        serial = max([int(name.split('-')[1]) for name in used] + [0]) + 1
        attempt = target/f'attempt-{serial:03d}'
        target.mkdir(parents=True, exist_ok=True)
        command = [sys.executable,'-m','f1_training.validation','evaluate','--release',str(release.path),
                   '--checkpoint',checkpoint,'--selection',str(Path(args.selection).resolve()),
                   '--output',str(attempt),'--device',args.device]
        if args.engineering:
            command.append('--engineering')
        with (target/(attempt.name+'.log')).open('x') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        load_report(attempt/'report.json')
        reports.append(str(attempt/'report.json'))
    summary = {'schema':'f1_sweep_complete_v1', 'reports':reports,
               'report_files':{p:sha256(p) for p in reports}, 'automatic_selection':False}
    if (root/'complete.json').exists():
        require(json.loads((root/'complete.json').read_text()) == summary, 'sweep result changed')
    else:
        write_new(root/'complete.json', summary)
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    manifest = commands.add_parser('manifest')
    manifest.add_argument('--release', required=True)
    manifest.add_argument('--stage', choices=['pass_a','pass_b'])
    manifest.add_argument('--windows-per-episode', type=int)
    manifest.add_argument('--generation-seeds', type=int, nargs='+')
    manifest.add_argument('--flow-windows-per-episode', type=int)
    manifest.add_argument('--flow-seeds', type=int, nargs='+')
    manifest.add_argument('--engineering', action='store_true')
    manifest.add_argument('--output', required=True)
    manifest.add_argument('--bound-release', help='Write a new release with this selection, preserving all approval flags')
    evaluate = commands.add_parser('evaluate')
    for option in ('release','checkpoint','output'):
        evaluate.add_argument('--'+option, required=True)
    evaluate.add_argument('--selection')
    evaluate.add_argument('--device', default='cuda:0')
    evaluate.add_argument('--engineering', action='store_true')
    compare = commands.add_parser('compare')
    compare.add_argument('--reports', nargs='+', required=True)
    compare.add_argument('--bootstrap-repeats', type=int, required=True)
    compare.add_argument('--seed', type=int, required=True)
    compare.add_argument('--output', required=True)
    sweep = commands.add_parser('sweep')
    for option in ('release','selection','output'):
        sweep.add_argument('--'+option, required=True)
    sweep.add_argument('--checkpoints', nargs='+', required=True)
    sweep.add_argument('--device', default='cuda:0')
    sweep.add_argument('--engineering', action='store_true')
    review = commands.add_parser('review')
    review.add_argument('--report', required=True)
    review.add_argument('--output', required=True)
    review.add_argument('--limit', type=int, default=12)
    decision = commands.add_parser('decision')
    decision.add_argument('--comparison', required=True)
    decision.add_argument('--review', required=True, help='Human-written JSON with primary/fallback and review reasons')
    decision.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'sweep':
        import fcntl
        root=Path(args.output); root.mkdir(parents=True,exist_ok=True)
        with (root/'.sweep.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_sweep(args)
        return
    if args.command == 'review':
        from .validation_review import build_review
        build_review(args.report, args.output, limit=args.limit)
        return
    if args.command == 'decision':
        from .validation_review import record_decision
        record_decision(args.comparison, args.review, args.output)
        return
    if args.command == 'compare':
        write_new(args.output, compare_reports(args.reports, repeats=args.bootstrap_repeats, seed=args.seed))
        return
    release = Release(args.release, engineering=args.engineering)
    if args.command == 'manifest':
        explicit = [args.windows_per_episode, args.generation_seeds, args.flow_windows_per_episode, args.flow_seeds]
        if args.stage:
            require(all(v is None for v in explicit), 'use a recipe stage OR explicit engineering counts/seeds')
            selection = build_recipe_selection(release, args.stage)
        else:
            require(args.engineering and all(v is not None for v in explicit), 'formal manifests require --stage; custom engineering manifests require explicit counts/seeds')
            selection = build_selection(release, windows_per_episode=args.windows_per_episode,
                                        generation_seeds=args.generation_seeds, flow_windows_per_episode=args.flow_windows_per_episode,
                                        flow_seeds=args.flow_seeds)
        release.document['evaluation'] = selection
        from .evaluation import validate_selection
        validate_selection(release)
        release_path=release.path
        if args.bound_release:
            require(args.stage=='pass_a', 'bind the training release to Pass A only')
            # Preserve file identities when the new release lives in another directory.
            doc=json.loads(json.dumps(release.document))
            doc['statistics']['path']=str(release.stats_path)
            doc['image_root']=str(release.image_root)
            for entry in doc['episodes']:
                for key in ('source','source_metadata','poses','pose_metadata','images'):
                    entry[key]['path']=str((release.path.parent/entry[key]['path']).resolve())
            write_new(args.bound_release,doc)
            release_path=Path(args.bound_release)
        write_new(args.output, {'release_sha256':sha256(release_path), 'selection':selection})
        return
    from .experiment import read_recipe, RECIPE_PATH
    from .evaluation import evaluate_model
    if args.selection:
        selection = json.loads(Path(args.selection).read_text())
        require(selection['release_sha256'] == sha256(release.path), 'selection/release mismatch')
        release.document['evaluation'] = selection['selection']
    checkpoint = Path(args.checkpoint).resolve()
    identity = validate_checkpoint(checkpoint, release)
    binding = identity['binding']
    import torch
    from transformers import AutoProcessor
    from opendm.exp.dm05_exp import DM05ModelConfig
    recipe = read_recipe()
    model = DM05ModelConfig(model_name_or_path=str(checkpoint), **recipe['model']).build_model(use_lora=False).to(args.device).eval()
    # Processor comes from the original base, never a G2 adapter or its stats.
    processor = AutoProcessor.from_pretrained(binding['base_model'], local_files_only=True)
    from contextlib import nullcontext
    autocast = (lambda: torch.autocast('cuda', dtype=torch.bfloat16)) if args.device.startswith('cuda') else nullcontext
    evaluate_model(model, release, processor, args.output, device=args.device, autocast=autocast,
                   checkpoint_identity=identity)


if __name__ == '__main__':
    main()
