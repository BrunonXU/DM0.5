"""Explicit user decisions. Separate from evidence and never overwritten by a model."""
import hashlib
from hub import HubError, now


def action_key(title):
    return 'next:' + hashlib.sha256(title.encode()).hexdigest()[:20]


def decisions(hub):
    with hub.connection() as conn:
        return hub.meta(conn, 'whereweare_decisions', [])


def save_decision(hub, snapshot, data):
    if set(data) - {'project_id', 'object_key', 'status', 'note'} or data.get('status') not in {'active', 'paused', 'done'}:
        raise HubError('需要 project_id、object_key 和 active/paused/done 状态')
    pid, key = data.get('project_id'), data.get('object_key')
    project = next((p for p in (snapshot or {}).get('projects', []) if p['id'] == pid), None)
    if not project or not isinstance(key, str):
        raise HubError('项目或对象不存在', 404)
    event = next((e for e in snapshot.get('events', []) if e['project_id'] == pid and key == 'event:' + e['id']), None)
    if event:
        title = event['title']
    elif key == action_key(project.get('next_step', '')) and project.get('next_step'):
        title = project['next_step']
    else:
        # Existing decisions remain addressable after a later review removes a suggestion.
        old = next((d for d in decisions(hub) if d['project_id'] == pid and d['object_key'] == key), None)
        if not old:
            raise HubError('对象已变化，请刷新后再决定', 409)
        title = old['title']
    note = data.get('note', '')
    if not isinstance(note, str) or len(note) > 500:
        raise HubError('决定说明最多500字')
    with hub.mutation() as conn:
        items = hub.meta(conn, 'whereweare_decisions', [])
        old = next((d for d in items if d['project_id'] == pid and d['object_key'] == key), None)
        history = list(old.get('history', [])) if old else []
        if old:
            history.append({k: old[k] for k in ('status', 'note', 'updated_at')})
        result = {'project_id': pid, 'object_key': key, 'title': title, 'status': data['status'],
                  'note': note, 'updated_at': now(), 'history': history}
        items = [d for d in items if not (d['project_id'] == pid and d['object_key'] == key)] + [result]
        hub.set_meta(conn, 'whereweare_decisions', items)
    return result
