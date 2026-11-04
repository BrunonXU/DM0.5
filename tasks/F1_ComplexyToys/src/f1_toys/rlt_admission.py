"""Durable per-worker receipts; a live learner is not proof of data admission."""
import json
import os
from pathlib import Path
import time
import uuid


def record_admission(health_root, episode, worker, status, **details):
    if not health_root:
        return
    if worker not in ('rollout', 'actor', 'learner') or Path(episode).name != episode or episode in ('', '.', '..'):
        raise ValueError('Invalid admission identity')
    root = Path(health_root) / 'admission' / episode
    root.mkdir(parents=True, exist_ok=True)
    value = dict(episode_id=episode, worker=worker, status=status,
                 updated_unix=time.time(), **details)
    temporary = root / (worker + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    os.replace(temporary, root / (worker + '.json'))


def admission_summary(health_root):
    episodes = []
    for path in sorted((Path(health_root) / 'admission').glob('*')):
        roles = {name: json.loads((path / (name + '.json')).read_text())
                 for name in ('rollout', 'actor', 'learner') if (path / (name + '.json')).exists()}
        rollout, actor = roles.get('rollout'), roles.get('actor')
        # A later failed/revised source must not be hidden by an older buffered
        # receipt. Separate files avoid env/actor writes racing over one status.
        current = actor or rollout
        consumed = roles.get('learner')
        if actor and consumed and consumed.get('manifest_sha256') == actor.get('manifest_sha256'):
            current = dict(actor, training_consumption=consumed)
        if rollout and actor and (rollout.get('source_revision') != actor.get('source_revision')
                                  or rollout['status'] in ('rejected', 'pending_review')):
            current = rollout
        if current:
            episodes.append(dict(current, worker_receipts=roles))
    return dict(episodes=episodes, rejected=sum(x['status'] == 'rejected' for x in episodes),
                preparing=sum(x['status'] == 'preparing' for x in episodes),
                buffered=sum(x['status'] == 'buffered' for x in episodes))
