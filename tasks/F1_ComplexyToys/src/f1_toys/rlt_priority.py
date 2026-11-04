"""Small, immutable learning bundles; originals remain the archival source.

Journal prefixes and image bytes keep their original hashes. The image index
is not rewritten: original pack offsets are resolved via a separate manifest.
No torch, network, robot output or model dependencies are required to pack.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .rlt_local_replay import read_prefix, build_transitions, OBJECTIVE
from .rlt_decision_replay import required_decision_observations

SCHEMA = 'f1_local_learning_bundle_v1'
OBSERVATION_SELECTION = 'legacy_and_decision_anchors_v2'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError('Learning payload path escaped source')
    return path


def effective_result(receipt, source_digest, review=None):
    result = dict(receipt['local_result'])
    if review is not None:
        if (review.get('source_receipt_sha256') != source_digest
                or review.get('objective') != OBJECTIVE
                or not str(review.get('reviewer') or '').strip()
                or review.get('approved') is not True
                or review.get('status') not in ('success', 'failure', 'pending_review')
                or (review['status'] == 'success' and review.get('placement_release_confirmed') is not True)):
            raise ValueError('Local review is not bound/approved for this source')
        if review['status'] == 'pending_review':
            cutoff = review.get('verified_nonterminal_until')
            if cutoff is None:
                return result
            if (review.get('nonterminal_confirmed') is not True
                    or not isinstance(cutoff, dict)
                    or not result['started_ns'] < cutoff['available_monotonic_ns'] <= result['ended_ns']):
                raise ValueError('Pending prefix needs a verified preterminal boundary')
            result.update(status='nonterminal_prefix', reward=0., terminated=False, truncated=True,
                          nonterminal_confirmed=True, terminal_observation=cutoff,
                          ended_ns=cutoff['available_monotonic_ns'])
            return result
        result.update(status=review['status'], reward=float(review['status'] == 'success'),
                      terminated=True, truncated=False, terminal_observation=review['terminal_observation'])
    return result


def read_frame(payload_root, frame):
    """Return the same verified JPEG bytes for raw packs or priority bundles."""
    root = Path(payload_root)
    marker = root.parent / 'learning-bundle.json'
    if marker.exists():
        # validate_bundle verifies membership before feature construction. Each
        # read still verifies length/hash, including partial or corrupt copies.
        name = frame['payload_sha256']
        if len(name) != 64 or any(c not in '0123456789abcdef' for c in name):
            raise ValueError('Invalid image content identity')
        path, offset = contained(root, 'blobs/'+name), 0
    else:
        path, offset = contained(root, frame['path']), frame.get('payload_offset', 0)
    if offset < 0 or frame['payload_bytes'] <= 0:
        raise ValueError('Invalid image byte range')
    with path.open('rb') as handle:
        handle.seek(offset)
        blob = handle.read(frame['payload_bytes'])
    if len(blob) != frame['payload_bytes'] or digest(blob) != frame['payload_sha256']:
        raise ValueError('Image prefix not yet complete or corrupt')
    return blob


def validate_bundle(run):
    """A transferred directory is invisible until its entire manifest verifies."""
    root = Path(run)
    manifest = json.loads((root/'learning-bundle.json').read_text())
    if manifest.get('schema') != SCHEMA or manifest.get('archive_complete') is not False:
        raise ValueError('Invalid learning bundle contract')
    for name, expected in manifest['files_sha256'].items():
        if digest(contained(root, name).read_bytes()) != expected:
            raise ValueError('Incomplete/corrupt learning bundle: '+name)
    receipt_path = root/'local_segments/placement-1.json'
    if digest(receipt_path.read_bytes()) != manifest['source_receipt_sha256']:
        raise ValueError('Learning bundle receipt identity mismatch')
    receipt = json.loads(receipt_path.read_text())
    if receipt['episode_id'] != manifest['episode_id']:
        raise ValueError('Learning bundle episode mismatch')
    streams = read_prefix(root, receipt)
    index = [json.loads(line) for line in (root/'payload/frames.jsonl').read_bytes().splitlines()]
    if sorted(row['observation_id'] for row in index) != manifest['observation_ids']:
        raise ValueError('Learning bundle observation membership mismatch')
    obs = {row['observation_id']: row for row in streams['observations']}
    for row in index:
        journal = obs[row['observation_id']]
        if row['state16'] != journal['state16'] or row['available_monotonic_ns'] != journal['available_monotonic_ns']:
            raise ValueError('Learning payload differs from journal')
        for frame in row['frames']:
            relative = 'payload/blobs/'+frame['payload_sha256']
            if manifest['files_sha256'].get(relative) != frame['payload_sha256']:
                raise ValueError('Learning image lacks manifest membership')
            read_frame(root/'payload', frame)
    return manifest


def build_bundle(run, destination_root, *, review_path=None):
    """Build and atomically commit one receipt revision, including its terminal.

    Called after local close, without waiting for Y. Only completed image-index
    lines and verified payloads are consumed. Retry is safe; never edits raw data.
    """
    run, destination_root = Path(run), Path(destination_root)
    receipt_data = (run/'local_segments/placement-1.json').read_bytes()
    receipt = json.loads(receipt_data)
    source_digest = digest(receipt_data)
    review_data = Path(review_path).read_bytes() if review_path else None
    review = json.loads(review_data) if review_data else None
    result = effective_result(receipt, source_digest, review)
    streams = read_prefix(run, receipt)
    records = build_transitions(streams, result)
    wanted = required_decision_observations(streams, result, records)
    revision = digest(receipt_data+(review_data or b'')+OBSERVATION_SELECTION.encode())
    destination_root.mkdir(parents=True, exist_ok=True)
    output = destination_root/revision
    if output.exists():
        validate_bundle(output)
        return output
    source = run/'payload' if (run/'payload').is_dir() else run/'payload.partial'
    index_data = (source/'frames.jsonl').read_bytes()
    lines = index_data[:index_data.rfind(b'\n')+1].splitlines(keepends=True)
    index = {}
    for line in lines:
        row = json.loads(line)
        if row['observation_id'] in wanted:
            if row['observation_id'] in index:
                raise ValueError('Duplicate image observation')
            index[row['observation_id']] = (row, line)
    if sorted(index) != wanted:
        raise ValueError('Required image index not yet complete')
    temporary = Path(tempfile.mkdtemp(prefix='.'+revision+'-', dir=destination_root))
    try:
        (temporary/'local_segments').mkdir()
        (temporary/'local_segments/placement-1.json').write_bytes(receipt_data)
        (temporary/'journal').mkdir()
        journal = run/'journal' if (run/'journal').is_dir() else run/'journal.partial'
        for name in streams:
            info = receipt['streams'][name]
            with (journal/(name+'.jsonl')).open('rb') as handle:
                data = handle.read(info['bytes'])
            if digest(data) != info['sha256']:
                raise ValueError('Source prefix changed during packing')
            (temporary/'journal'/(name+'.jsonl')).write_bytes(data)
        (temporary/'payload/blobs').mkdir(parents=True)
        (temporary/'payload/frames.jsonl').write_bytes(b''.join(index[k][1] for k in wanted))
        for row, _ in index.values():
            for frame in row['frames']:
                blob = read_frame(source, frame)
                (temporary/'payload/blobs'/digest(blob)).write_bytes(blob)
        if review_data:
            (temporary/'source-review.json').write_bytes(review_data)
        files = {str(p.relative_to(temporary)): digest(p.read_bytes())
                 for p in temporary.rglob('*') if p.is_file()}
        manifest = dict(schema=SCHEMA, archive_complete=False, episode_id=receipt['episode_id'],
                        source_receipt_sha256=source_digest,
                        review_sha256=digest(review_data) if review_data else None,
                        observation_selection=OBSERVATION_SELECTION,
                        observation_ids=wanted, transition_count=len(records),
                        source_index_prefix_sha256=digest(index_data[:index_data.rfind(b'\n')+1]),
                        files_sha256=files)
        (temporary/'learning-bundle.json').write_text(json.dumps(manifest, indent=2)+'\n')
        validate_bundle(temporary)
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return output
