"""Versioned episode eligibility. Unknown data waits for review; raw stays intact."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


def load_policy(path, expected_sha256=None):
    raw = Path(path).read_bytes()
    if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Data selection policy changed after launch')
    value = json.loads(raw)
    if value.get('schema') != 'f1_rlt_data_policy_v1' or value.get('default') != 'quarantine':
        raise ValueError('Expected fail-closed episode selection')
    for name, entry in value['episodes'].items():
        if Path(name).name != name or entry.get('split') not in ('train', 'validation', 'exclude'):
            raise ValueError('Invalid episode selection')
        if not entry.get('reason') or len(entry.get('source_receipt_sha256', '')) != 64:
            raise ValueError('Selection requires source identity and reason')
    if not any(e['split'] == 'validation' for e in value['episodes'].values()):
        raise ValueError('Independent validation episode required')
    return value


def select_receipt(policy, value, digest):
    if value.get('binding', {}).get('collection_purpose') == 'evaluation':
        return 'evaluation'  # Cannot be overridden by normal train selection.
    entry = policy['episodes'].get(value['episode_id'])
    if entry is not None:
        if digest != entry['source_receipt_sha256']:
            raise ValueError('Selected episode receipt changed')
        return entry['split']
    future = policy.get('future')
    if not future:
        return 'quarantine'
    import re
    match = re.fullmatch(r'f1_hil_(\d{8}T\d{6}Z)_[0-9a-f]+', value['episode_id'])
    binding = value.get('binding', {})
    if (not match or match[1] < future['not_before_utc']
            or value.get('schema') != 'f1_local_journal_prefix_v1'
            or binding.get('recording_boundary') not in ('f1_rlt_local_v2', 'f1_rlt_local_v3', 'f1_rlt_local_v4')
            or binding.get('execution_mode') != 'powered'
            or binding.get('robot') != 'F1-09'
            or binding.get('base_model_version') != future['base_model_version']
            or binding.get('rlt', {}).get('encoder_manifest_sha256') != future['encoder_manifest_sha256']):
        return 'quarantine'
    # Split is assigned before looking at success/failure, invariant on retry.
    modulus = int(future['validation_modulus'])
    if modulus < 2:
        raise ValueError('Future validation partition must leave training episodes')
    key = hashlib.sha256(value['episode_id'].encode()).hexdigest()
    return 'validation' if int(key[:16], 16) % modulus == 0 else 'train'


def episode_selection(policy, receipt_path):
    raw = Path(receipt_path).read_bytes()
    return select_receipt(policy, json.loads(raw), hashlib.sha256(raw).hexdigest())
