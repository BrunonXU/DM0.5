"""Source-bound DAgger seed view; original recordings keep their capture mode."""
import json
from pathlib import Path

import torch

from .rlt_online import sha


def load_demo_seed(config, feature_identity):
    root, audit_root = Path(config['root']), Path(config['audit_root'])
    if sha(root/'manifest.json') != config['manifest_sha256']:
        raise ValueError('Demo seed manifest changed')
    manifest = json.loads((root/'manifest.json').read_text())
    if (manifest.get('schema') != 'f1_rlt_reviewed_demo_seed_tensors_v1'
            or manifest.get('objective') != 'approach_place_release_v1'
            or manifest.get('capture_mode') != 'DAgger'
            or manifest.get('learning_use') != 'RLT_demo_seed'
            or manifest.get('feature_identity') != feature_identity
            or sha(audit_root/'audit.json') != manifest.get('audit_sha256')):
        raise ValueError('Demo seed source/feature identity mismatch')
    audit = json.loads((audit_root/'audit.json').read_text())
    for directory, meta in ((root, manifest), (audit_root, audit)):
        for name, digest in meta['files_sha256'].items():
            if Path(name).name != name or sha(directory/name) != digest:
                raise ValueError('Demo seed file checksum mismatch')
    rows = [json.loads(line) for line in (audit_root/'transitions.jsonl').read_text().splitlines()]
    data, records, groups = {}, {}, {}
    for split in ('train', 'validation'):
        records[split] = [r for r in rows if r['split'] == split]
        groups[split] = {r['source_run'] for r in records[split]}
        data[split] = torch.load(root/(split+'.pt'), map_location='cpu', weights_only=True)
        part = data[split]; n = len(records[split])
        if n != manifest['counts'][split] or n != len(part['actions']):
            raise ValueError('Demo tensor/record count mismatch')
        for value in part.values():
            for tensor in (value.values() if isinstance(value, dict) else (value,)):
                if len(tensor) != n or not torch.isfinite(tensor).all():
                    raise ValueError('Invalid demo tensor')
        from .rlt_action_contract import DECISION
        aligned=manifest.get('action_representation')==DECISION
        masks_valid=(torch.equal(part['intervene_flags'],part['action_time_mask'])
                     and part['action_time_mask'][:,0].all()
                     and not (part['action_time_mask'][:,1:] & ~part['action_time_mask'][:,:-1]).any()) if aligned else (
                         part['action_time_mask'].all() and part['intervene_flags'].all())
        if (tuple(part['actions'].shape) != (n, 16, 10)
                or not masks_valid
                or not torch.equal(part['terminated'], torch.tensor([r['terminated'] for r in records[split]]))
                or not torch.equal(part['reward'], torch.tensor([sum(r['rewards']) for r in records[split]]))):
            raise ValueError('Demo execution/outcome labels disagree with audit')
        # This loader verifies the source-bound, reviewed seed audit above.
        part['expert_approved'] = part['intervene_flags'] & part['action_time_mask']
    if groups['train'] & groups['validation']:
        raise ValueError('Demo validation leakage')
    train_groups = {r['source_group'] for r in records['train']}
    if train_groups & {r['source_group'] for r in records['validation']}:
        raise ValueError('Demo source group validation leakage')
    return data, records, manifest
