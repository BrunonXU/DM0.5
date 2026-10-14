"""Offline import, review and official v3 export; never controls a robot."""
import argparse
import json
from pathlib import Path
from .adapters import canonical, f710, inspect_source
from .contract import atomic_json
from .writer import export, review_template


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=('inspect', 'review-template', 'export'))
    p.add_argument('--source', required=True)
    p.add_argument('--dagger-root')
    p.add_argument('--lerobot-root')
    p.add_argument('--fps', type=int, default=30)
    p.add_argument('--output')
    p.add_argument('--review')
    a = p.parse_args()
    if a.operation == 'inspect':
        result = inspect_source(a.source)
    else:
        if (Path(a.source)/'capture.json').is_file():
            bundle = canonical(a.source, fps=a.fps)
        elif a.dagger_root:
            bundle = f710(a.source, a.dagger_root, fps=a.fps)
        else:
            p.error('F710 requires --dagger-root; legacy MCAP/27D require provenance audit before export')
        if a.operation == 'review-template':
            result = review_template(bundle)
            if a.output:
                path = Path(a.output)
                if path.exists(): raise FileExistsError(path)
                path.parent.mkdir(parents=True, exist_ok=True); atomic_json(path, result)
        else:
            if not a.output or not a.lerobot_root: p.error('export requires --output and --lerobot-root')
            review = json.loads(Path(a.review).read_text()) if a.review else None
            result = export(bundle, a.output, a.lerobot_root, review=review)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
