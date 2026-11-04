"""Read-only policy recording classification; raw identities and files stay intact."""
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def model_registry():
    return json.loads((Path(__file__).parent/'configs/rollout_models.json').read_text())


@lru_cache(maxsize=1024)
def event_interval(path, size, mtime_ns):
    """Bounded event-only read, cached by file identity; never scans control/images."""
    if size > 64*1024*1024:
        return None
    start = end = None
    with open(path) as stream:
        for line in stream:
            item = json.loads(line)
            ns = item.get('monotonic_ns')
            if not isinstance(ns, int):
                continue
            kind = item.get('event')
            if kind == 'local_policy_authorized' and start is None:
                start = ns
            if kind in ('standalone_finished', 'session_termination') and end is None and start is not None and ns >= start:
                end = ns
    return (end-start)/1e9 if start is not None and end is not None else None


CAPTURE_LABELS = {'rlt': 'RLT', 'dagger': 'DAgger', 'demonstration': '人工示教',
                  'rollout': '真机Rollout', 'shadow': 'Shadow测试', 'unknown': '待确认来源'}


def tag_capture(row, mode, evidence):
    """Source tags describe launch mode, never training approval or success."""
    explicit = row.get('binding', {}).get('capture_mode')
    if explicit is not None and explicit != mode:
        mode, evidence = 'unknown', ['binding.capture_mode conflicts with recorded launch identity']
        row.update(recording_kind='unknown', library_folder='其他录制/待确认来源')
    row.update(capture_mode=mode, capture_label=CAPTURE_LABELS[mode],
               capture_classification=dict(schema='f1_capture_classification_v1',
                   capture_mode=mode, label=CAPTURE_LABELS[mode],
                   source_manifest_sha256=row.get('metadata_sha256'),
                   evidence=evidence, classifier_version=1,
                   training_eligibility='separate_review_required'))
    return row


def decorate(row, run, manifest):
    """Classify by launch binding, independently of model training or folders."""
    if row.get('source') not in ('hil_demonstration', 'hil_policy_hil'):
        return row
    binding = row.get('binding', {})
    registry = model_registry()
    if binding.get('execution_mode') == 'shadow':
        row.update(recording_kind='shadow', library_folder='其他录制/Shadow测试')
        return tag_capture(row, 'shadow', ['binding.execution_mode=shadow'])
    if row['source'] == 'hil_demonstration':
        row.update(recording_kind='demonstration', library_folder='HIL/人工示教')
        return tag_capture(row, 'demonstration', ['binding.purpose=demonstration'])
    rlt = binding.get('rlt')
    if rlt:
        if isinstance(rlt, dict) and rlt.get('control_mode') == 'rlt_hil_supervised':
            row.update(recording_kind='rlt', library_folder='HIL/RLT')
            return tag_capture(row, 'rlt', ['binding.rlt.control_mode=rlt_hil_supervised'])
        row.update(recording_kind='unknown', library_folder='其他录制/待确认来源')
        return tag_capture(row, 'unknown', ['unrecognized binding.rlt'])
    if binding.get('rtc_hil'):
        row.update(recording_kind='hil', library_folder='HIL/DAgger')
        return tag_capture(row, 'dagger', ['binding.rtc_hil=true', 'no RLT launch binding'])
    standalone = binding.get('standalone_rtc') or binding.get('standalone_sync')
    legacy = row['run_id'] in registry.get('legacy_evaluations', [])
    if binding.get('execution_mode') != 'powered' or not (standalone or legacy):
        row.update(recording_kind='unknown', library_folder='其他录制/待确认来源')
        return tag_capture(row, 'unknown', ['no recognized launch identity'])
    model_id = str(binding.get('model_id') or 'unknown')
    safe_id = re.sub(r'[^A-Za-z0-9_.-]', '_', model_id)[:96]
    model = registry['models'].get(model_id)
    checkpoint = model['label'] if model else '未登记模型_'+safe_id
    seconds = None
    events = run/'journal/events.jsonl'
    try:
        st = events.stat()
        if events.is_symlink():
            raise ValueError('symlink event stream')
        seconds = event_interval(str(events), st.st_size, st.st_mtime_ns)
    except (OSError, ValueError, TypeError):
        pass
    # Recording date is the journal wall-clock start; execution excludes Enter wait.
    stamp = datetime.fromisoformat(row['started_at']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    elapsed = f'执行 {seconds:.1f}秒' if seconds is not None else '执行时长未记录'
    row.update(recording_kind='rollout', library_folder='真机Rollout/'+checkpoint,
               name=f'{stamp} · {elapsed}', checkpoint_label=checkpoint,
               execution_duration_s=seconds, display_timezone='Asia/Shanghai')
    return tag_capture(row, 'rollout', ['standalone launch' if standalone else 'registered legacy evaluation'])
