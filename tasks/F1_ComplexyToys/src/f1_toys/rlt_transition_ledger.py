"""Stable transition budget identities across review/cache revisions."""
import hashlib
import json


def transition_id(record):
    fields = ('episode_id', 'segment_id', 'decision_kind', 'observation_id', 'next_observation_id',
              'decision_ns', 'next_decision_ns', 'action_time_mask', 'command_sequences')
    if not all(k in record for k in ('episode_id', 'decision_ns', 'next_decision_ns', 'command_sequences')):
        raise ValueError('Transition lacks stable execution identity')
    return hashlib.sha256(json.dumps({k:record.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def new_credits(credited, records):
    ids = {transition_id(r) for r in records}
    if len(ids) != len(records):
        raise ValueError('Duplicate transitions in one cache')
    return ids-set(credited), ids


def retracted_ids(old_rows, old_data, new_rows, new_data):
    """Changed executed targets/features or withdrawn expert approval need replay.

    Adding explicit expert supervision is allowed without pretending past
    reference-regularized gradients were expert gradients.
    """
    import torch
    old = {transition_id(r):i for i,r in enumerate(old_rows)}
    new = {transition_id(r):i for i,r in enumerate(new_rows)}
    invalid = set(old)-set(new)
    def same(a,b,i,j):
        if isinstance(a,dict) and isinstance(b,dict):
            return set(a)==set(b) and all(same(a[k],b[k],i,j) for k in a)
        return torch.equal(a[i],b[j])
    for key in set(old)&set(new):
        i,j=old[key],new[key]
        columns=set(old_data)|set(new_data)
        for name in columns-{'expert_approved'}:
            if name not in old_data or name not in new_data or not same(old_data[name],new_data[name],i,j):
                invalid.add(key);break
        previous=old_data.get('expert_approved',torch.zeros_like(old_data['action_time_mask']))[i]
        current=new_data.get('expert_approved',torch.zeros_like(new_data['action_time_mask']))[j]
        if (previous & ~current).any():invalid.add(key)
    return invalid
