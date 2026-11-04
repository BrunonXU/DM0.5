#!/usr/bin/env python3
"""Extract release-end candidates for review; never infer placement success."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.recipe.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    candidates = []
    for segment in cfg['human_segments']:
        name = segment['source_run']
        root = Path(cfg['human_raw_root'])/name
        review = json.loads((Path(cfg['human_review_root'])/(name+'.json')).read_text())
        phase = {p['stage']: p for p in review['phase_ranges']}
        release, home = phase['release']['start_ns'], phase['native_home']['start_ns']
        observations = [json.loads(line) for line in (root/'journal/observations.jsonl').read_text().splitlines()]
        frames = {r['observation_id']: r for r in map(json.loads, (root/'payload/frames.jsonl').read_text().splitlines())}
        opened = [r for r in observations if release <= r['available_monotonic_ns'] < home and r['state16'][15] <= 1]
        item = dict(source_run=name, original_review_sha256=segment['review_sha256'],
                    source_manifest_sha256=review['manifest_sha256'], objective='approach_place_release_v1',
                    placement_release_confirmed=False, reviewer=None, candidates=[])
        if opened:
            origin = opened[0]['available_monotonic_ns']
            selected = {}
            for delta in (0, 100_000_000, 300_000_000):
                row = next((r for r in opened if r['available_monotonic_ns'] >= origin+delta), None)
                if row:
                    selected[row['observation_id']] = row
            for obs in selected.values():
                images = []
                for frame in frames[obs['observation_id']]['frames']:
                    if frame['camera'] not in ('head', 'right_wrist'):
                        continue
                    path = (root/'payload'/frame['path']).resolve()
                    if (root/'payload').resolve() not in path.parents:
                        raise ValueError('Payload path escaped raw recording')
                    with path.open('rb') as handle:
                        handle.seek(frame.get('payload_offset', 0))
                        data = handle.read(frame['payload_bytes'])
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != frame['payload_sha256']:
                        raise ValueError('Raw image hash mismatch')
                    target = args.output/f'{name}-{obs["observation_id"]}-{frame["camera"]}.jpg'
                    target.write_bytes(data)
                    images.append(dict(path=target.name, sha256=digest))
                item['candidates'].append(dict(observation_id=obs['observation_id'],
                    available_monotonic_ns=obs['available_monotonic_ns'], right_gripper_feedback=obs['state16'][15],
                    state16=obs['state16'], images=images))
        candidates.append(item)
    (args.output/'review-candidates.json').write_text(json.dumps(candidates, indent=2)+'\n')


if __name__ == '__main__':
    main()
