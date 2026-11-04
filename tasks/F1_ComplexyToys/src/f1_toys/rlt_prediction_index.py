"""Read a bounded snapshot of original proposals; never treat them as actions."""
import hashlib
import json
from pathlib import Path

from .rlt_decision_replay import prediction_key


def source_predictions(path, plans):
    wanted={prediction_key(p) for p in plans}
    found={}
    paths = path if isinstance(path, (list, tuple)) else [path]
    # Writers may append during this read. Only complete lines present at open
    # belong to the snapshot; a missing adopted reply is retryable, not padding.
    for source in paths:
        path = Path(source)
        # The next service's log may not exist until its first request. Missing
        # required replies remain a retry, never a fabricated replay record.
        if not path.exists():
            continue
        with path.open('rb') as handle:
            limit=path.stat().st_size
            while handle.tell()<limit:
                line=handle.readline()
                if handle.tell()>limit or not line.endswith(b'\n'):break
                row=json.loads(line)
                if not row.get('success') or 'prediction_replay' not in row:continue
                key=prediction_key(row)
                if key not in wanted:continue
                row['_source_line_sha256']=hashlib.sha256(line).hexdigest()
                if key in found and found[key]!=row:raise ValueError('Conflicting original actor replies')
                found[key]=row
    if wanted-found.keys():raise ValueError('Adopted actor replies are not all available yet')
    return found
