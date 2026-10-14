"""Read-only adapters. An old file format never confers trustworthy action labels."""
from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import sys
from .contract import CAMERAS, CONTRACT, SCHEMA, NAMES, sha256, validate_row, confined
from .capture import read_capture, read_image


@dataclass
class Bundle:
    metadata: dict
    segments: list
    image_bytes: object
    verify_source: object = None


def load_dagger(root):
    root = Path(root).resolve(strict=True)
    platform = root/'F1' if (root/'F1/src/f1dagger').is_dir() else root
    for p in (platform/'src', root/'shared/src', platform.parent/'shared/src'):
        if p.is_dir() and str(p) not in sys.path: sys.path.insert(0, str(p))
    module = importlib.import_module('f1dagger.export')
    if platform not in Path(module.__file__).resolve().parents:
        raise ValueError('another f1dagger dependency is already loaded')
    return module


def f710(root, dagger_root, *, fps=30):
    """Reuse audited F710 boundary/causality selection; this is NOT human approval."""
    root = Path(root).resolve(strict=True)
    export = load_dagger(dagger_root)
    from f1dagger.recording import audit, read_rows
    from f1dagger.payload_archive import read_payload
    report = audit(root)
    if not report['valid']: raise ValueError('F710 source audit failed: '+str(report['errors']))
    controls = read_rows(root/'journal/control.jsonl')
    ids = sorted({c['intervention_id'] for c in controls if c.get('intervention_id', 0) > 0})
    # Existing selector requires explicit IDs. These IDs only select technical
    # candidates; the common writer independently requires human approval.
    technical_selection = dict(contract=CONTRACT, manifest_sha256=report['manifest_sha256'],
                               reviewer='software structural selection; not human approval', accepted_interventions=ids)
    groups, selection, manifest = export.select_segments(root, technical_selection, fps=fps)
    observations = {r['observation_id']: r for r in read_rows(root/'journal/observations.jsonl')}
    session = str(manifest.get('episode_id') or root.name)
    result = []
    for segment_index, group in enumerate(groups):
        rows = []
        for old in group:
            obs = observations[old['observation_id']]
            images = {f['camera']: dict(f, frame_id=f['frame_id'], received_ns=f['host_received_monotonic_ns']) for f in old['frames']}
            row = dict(contract=CONTRACT, time_ns=old['reference_monotonic_ns'], clock_basis='host_monotonic_receipt',
                       session_id=session, state=list(old['state']), action=list(old['action']), action_valid=[True]*16,
                       action_source='f1dagger_single_owner_publication', control_source='human', phase='human',
                       owner_epoch=old['policy_epoch'], intervention_id=old['intervention_id'],
                       source_row=old['observation_id'], source_command=old['command_sequence'],
                       source_action_timestamps_ns=old['action_source_timestamps_ns'],
                       state_timestamps_ns=old['state_timestamps_ns'], body_head_context=obs.get('full22'),
                       segment_id=str(segment_index), images=images)
            # Preserve fixed-grid vs real observation provenance from the
            # selector; a held frame must not become a claimed new exposure.
            for key in ('resampling', 'grid_index', 'source_observation_monotonic_ns',
                        'observation_age_ns', 'observation_reused', 'command_monotonic_ns'):
                if key in old:
                    row[key] = old[key]
            rows.append(validate_row(row))
        result.append(rows)
    metadata = dict(source_kind='f710', session_id=session, contract=CONTRACT, fps=fps,
                    robot_id=manifest['binding'].get('robot'), task=manifest['task_prompt'],
                    source_root=str(root), source_sha256=report['manifest_sha256'],
                    payload_index_sha256=sha256(root/'payload/frames.jsonl'),
                    synthetic=manifest['binding'].get('synthetic', False), selection=selection,
                    source_binding=manifest['binding'])
    def verify_source():
        current = audit(root)
        if not current['valid'] or current['manifest_sha256'] != metadata['source_sha256']:
            raise ValueError('F710 source changed during export')
    return Bundle(metadata, result, lambda row, camera: read_payload(root/'payload', row['images'][camera]), verify_source)


def canonical(root, *, fps=30):
    meta, rows = read_capture(root)
    from .selection import segments
    groups, quality = segments(rows, fps=fps)
    metadata = dict(meta, source_kind=meta.get('collection_method', 'pico'), source_root=str(Path(root).resolve()),
                    source_sha256=sha256(Path(root)/'capture.json'), fps=fps, selection=quality)
    def verify_source():
        read_capture(root)
        if sha256(Path(root)/'capture.json') != metadata['source_sha256']:
            raise ValueError('capture source changed during export')
    return Bundle(metadata, groups, lambda row, camera: read_image(root, row['images'][camera]), verify_source)


def inspect_source(root):
    root = Path(root).resolve(strict=True)
    if (root/'capture.json').is_file():
        m, rows = read_capture(root)
        return dict(format=SCHEMA, rows=len(rows), contract=m['contract'], source_sha256=sha256(root/'capture.json'),
                    source_kind=m.get('collection_method'), training_ready=False)
    if (root/'journal/manifest.json').is_file():
        m = json.loads((root/'journal/manifest.json').read_text())
        return dict(format='f1dagger_raw', contract=m.get('contract'), binding=m.get('binding', {}),
                    source_sha256=sha256(root/'journal/manifest.json'), training_ready=False)
    if (root/'metadata.yaml').is_file():
        import yaml
        m = yaml.safe_load(confined(root, 'metadata.yaml').read_text())['rosbag2_bagfile_information']
        topics = {r['topic_metadata']['name']: r['message_count'] for r in m['topics_with_message_count']}
        has_final = '/motion_ctl/joint_ctl' in topics
        return dict(format='rosbag2_'+m['storage_identifier'], topics=topics,
                    seconds=m['duration']['nanoseconds']/1e9, source_sha256=sha256(root/'metadata.yaml'),
                    action_source='final_ros_command_unverified_owner' if has_final else 'lead_command_only',
                    training_ready=False, blockers=['Legacy command ownership and action mapping require evidence'] +
                    ([] if has_final else ['Final arm command was not recorded; cannot reconstruct it by renaming lead']))
    if (root/'meta/info.json').is_file():
        m = json.loads(confined(root, 'meta/info.json').read_text())
        n = m.get('features', {}).get('action', {}).get('shape', [None])[0]
        return dict(format='legacy_lerobot', version=m.get('codebase_version'), dimensions=n, fps=m.get('fps'),
                    episodes=m.get('total_episodes'), features=m.get('features'),
                    source_sha256=sha256(root/'meta/info.json'), training_ready=False,
                    blockers=['Official reader compatibility unverified',
                              'Legacy per-row action origin/fallback validity requires original evidence'] +
                    (['27D must map named arms + grippers; moving body/base cannot be silently discarded'] if n == 27 else []))
    raise ValueError('unrecognized capture format')
