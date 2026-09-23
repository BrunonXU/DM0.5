"""Task entry points. inspect/prepare never load model weights or train."""
import argparse
import json
from pathlib import Path
from .data import Release, describe_release
from f1_data.reader import Episode, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare-engineering')
    for option in ('cases', 'pose-cache', 'statistics', 'images', 'output'):
        prepare.add_argument('--' + option, type=Path, required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--release', required=True)
    inspect.add_argument('--engineering', action='store_true')
    train = sub.add_parser('train')
    for option in ('release', 'base-model', 'output'):
        train.add_argument('--' + option, required=True)
    train.add_argument('--resume', default=None)
    args = parser.parse_args()
    if args.command == 'prepare-engineering':
        episodes = []
        for source in sorted(args.cases.glob('*/composed/episode.npz')):
            cache = args.pose_cache / source.parent.parent.name / 'pose_cache.npz'
            episodes.append(Episode(source, cache, allow_candidates=True))
        require(episodes, 'no episodes found')
        doc = describe_release(episodes, args.statistics, args.images)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        release = Release(args.output, engineering=True)
    elif args.command == 'inspect':
        release = Release(args.release, engineering=args.engineering)
    else:
        from .experiment import F1Exp
        F1Exp(args.release, args.base_model, args.output).train(resume=args.resume)
        return
    print(json.dumps({'training_approved': release.document['training_approved'],
                      'sources': len(release.episodes),
                      'windows': {split: sum(len(ep.starts) for ep in release.for_split(split))
                                  for split in ('train', 'validation')},
                      'model_loaded': False, 'training_started': False}, indent=2))


if __name__ == '__main__':
    main()
