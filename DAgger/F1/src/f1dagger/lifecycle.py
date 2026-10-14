"""Session-bound graceful finish; filesystem work stays off the control loop."""
import json
import os
from pathlib import Path
import time


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(data, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def session_root(runtime, data_root, purpose):
    if not data_root or purpose not in ('demonstration', 'policy_hil'):
        return Path(runtime)/'raw'
    path = Path(data_root).expanduser()
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('data root must be absolute and contain no symlinks')
    return path.resolve()/purpose


def finish_requested(path, identity, now=None):
    try:
        if path.stat().st_size > 4096:
            return False
        request = json.loads(path.read_text())
        age = (time.time() if now is None else now)-request['requested_unix']
        return (request.get('schema') == 'f1_finish_request_v1' and 0 <= age <= 10
                and all(request.get(k) == identity[k] for k in ('run_id', 'pid', 'start_ticks')))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def publish_completion(runtime, run, binding, phase, errors=()):
    """Only committed manifests may be presented as saved; never approves training."""
    result = dict(schema='f1_session_completion_v1', run_id=run.name, path=str(run),
                  purpose=binding.get('purpose'), phase=phase, updated_unix=time.time(),
                  training_ready=False, errors=list(errors))
    if phase == 'saved':
        try:
            m = json.loads((run/'journal/manifest.json').read_text())
            if not m.get('complete') or m.get('writer_error') or m.get('drop_count'):
                raise ValueError('journal did not commit cleanly')
            result['observations'] = m['counts']['observations']
            termination = m.get('binding', {}).get('termination')
            if termination:
                result.update(outcome=termination['outcome'], reason=termination['reason'],
                              requires_review=True)
            if binding.get('recording_mode') != 'inference-only':
                p = json.loads((run/'payload/manifest.json').read_text())
                if not p.get('complete') or p.get('writer_error') or p.get('drop_count') or p.get('count') != result['observations']:
                    raise ValueError('payload did not commit with all observations')
            else:
                result['phase'] = 'ended'
        except (OSError, ValueError, KeyError) as exc:
            result.update(phase='failed', errors=[str(exc)])
    atomic(Path(runtime)/'last-session.json', result)
    return result
