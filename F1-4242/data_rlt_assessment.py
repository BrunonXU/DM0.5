"""Read-only RLT outcome projection, independent of capture mode and user notes.

Training reviews are source-bound audit/admission snapshots, not evidence that a
live learner has consumed an episode or completed any gradient update. Reads
never write a sidecar and never infer rewards from editable prose.
"""
from functools import lru_cache
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat

import data_annotations as annotations
from core import atomic_json

SCHEMA = 'f1_rlt_assessment_v1'
REVIEW_SCHEMA = 'f1_rlt_training_review_v1'
MAX_EVENTS_BYTES = 64 * 1024 * 1024
MAX_EVENT_LINES = 100_000
MAX_LINE_BYTES = 128 * 1024
MAX_SEGMENTS = 256
OUTCOMES = {
    'assisted_success': '人工辅助成功', 'autonomous_success': '自主成功',
    'failure': '局部失败', 'interrupted': '局部中断，待审核',
    'not_entered': '未进入局部RLT', 'unknown': '结果未确认',
    'mixed': '含多个局部结果',
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _file_key(path):
    """A changed inode, content stat, or missing file invalidates the read cache."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('symbolic recording evidence path')
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('non-regular recording evidence')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read_json(path, limit=1_000_000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('evidence size/type limit')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('evidence grew beyond limit')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('evidence must be an object')
    return value, hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=512)
def _read_evidence(root, manifest_key, event_key, control_key, annotation_key):
    """Parse only bounded metadata/events, never control rows or image packs."""
    run = Path(root)
    manifest, manifest_sha = _read_json(run / 'journal/manifest.json')
    if not isinstance(manifest.get('counts', {}), dict) or not isinstance(manifest.get('binding', {}), dict):
        raise ValueError('原件计数或来源结构无效')
    document = annotations.read_document(run / annotations.NAME) if annotation_key else {}
    result = dict(manifest=manifest, manifest_sha256=manifest_sha, document=document,
                  recording_start_ns=None, segments=[], old_save=False,
                  event_count=0, event_error=None)
    if event_key is None:
        result['event_error'] = '缺少事件日志'
        return result
    if event_key[2] > MAX_EVENTS_BYTES:
        result['event_error'] = '事件日志超过只读摘要上限，需离线审核'
        return result
    opened, closed = {}, {}
    try:
        fd = os.open(run / 'journal/events.jsonl', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                result['event_count'] += 1
                if len(line) > MAX_LINE_BYTES or result['event_count'] > MAX_EVENT_LINES:
                    raise ValueError('事件日志超过只读摘要上限，需离线审核')
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError('事件日志格式无效')
                episode = item.get('episode_id')
                if episode and episode != manifest.get('episode_id'):
                    raise ValueError('事件日志来源不一致')
                kind = item.get('event')
                ns = item.get('monotonic_ns')
                if kind == 'demonstration_started' and isinstance(ns, int):
                    if result['recording_start_ns'] is None:
                        result['recording_start_ns'] = ns
                if kind == 'episode_end_accepted':
                    result['old_save'] = True
                if kind not in ('rlt_local_opened', 'rlt_local_closed'):
                    continue
                started = item.get('started_ns')
                if not _number(started) or started < 0 or not _number(ns) or ns < started:
                    raise ValueError('局部事件时间无效')
                if kind == 'rlt_local_closed':
                    ended = item.get('ended_ns')
                    if not _number(ended) or ended < started:
                        raise ValueError('局部结束时间无效')
                    terminal = item.get('terminal_observation')
                    if terminal is not None and not isinstance(terminal, dict):
                        raise ValueError('局部终点结构无效')
                segment_id = item.get('segment_id')
                if not isinstance(segment_id, str) or not segment_id:
                    raise ValueError('局部事件缺少段身份')
                target = opened if kind == 'rlt_local_opened' else closed
                if segment_id in target and target[segment_id] != item:
                    raise ValueError('局部事件身份重复冲突')
                target[segment_id] = item
                if len(set(opened) | set(closed)) > MAX_SEGMENTS:
                    raise ValueError('局部段超过摘要上限')
        for segment_id in dict.fromkeys([*opened, *closed]):
            start, end = opened.get(segment_id), closed.get(segment_id)
            if not start:
                raise ValueError('局部结束事件缺少开始事件')
            if end and start.get('started_ns') != end.get('started_ns'):
                raise ValueError('局部起止身份不一致')
            result['segments'].append(dict(segment_id=segment_id, opened=start, closed=end))
    except (OSError, ValueError, TypeError) as error:
        result['event_error'] = str(error)
        result['segments'] = []  # Never project a partial scan as a complete result.
    return result


def _seconds(ns, start):
    return round((ns - start) / 1e9, 6) if _number(ns) and _number(start) and ns >= start else None


def _raw_outcome(segment):
    closed = segment.get('closed')
    if not closed:
        return 'interrupted'
    status = closed.get('status')
    reward = closed.get('reward')
    if status == 'success' and _number(reward) and reward == 1 and closed.get('terminated') is True:
        # Missing assistance metadata must never become an autonomous success.
        return 'assisted_success' if closed.get('assisted') is True else ('autonomous_success' if closed.get('assisted') is False else 'unknown')
    if status == 'failure' and _number(reward) and reward == 0 and closed.get('terminated') is True:
        return 'failure'
    return 'interrupted' if status == 'interrupted' else 'unknown'


def _default(recording):
    return dict(schema=SCHEMA, outcome='unknown', outcome_label=OUTCOMES['unknown'],
                confirmation_label='无局部成功确认', confirmation_kind='none', usage_label='待审核', split='pending',
                transition_count=None, retained_seconds=None, notes_summary='',
                original_status=None, source_label='原始事件', reviewed_at=None,
                local_start_s=None, terminal_s=None, original_end_s=None,
                terminal_observation_id=None, excluded_summary='', quality='pending_review',
                source_signature=recording.get('source_signature'), source_bound=False,
                snapshot=False, local_segment_count=None, local_segments=[],
                status_label='待审核', evidence_refs=[],
                snapshot_notice='审核与训练准入快照，不代表实时训练已完成')


def _pending(result, quality, label, detail):
    result.update(quality=quality, split='pending', usage_label=label, status_label=label,
                  notes_summary=detail, transition_count=None, retained_seconds=None,
                  snapshot=False, source_bound=False)
    return result


def _review_error(review, recording):
    if not isinstance(review, dict) or review.get('schema') != REVIEW_SCHEMA:
        return 'review_invalid', '训练审核格式不完整'
    if review.get('requires_remote_prefix_verification') or review.get('requires_remote_policy_verification'):
        return 'review_invalid', '训练来源前缀或数据策略尚未完成核验'
    for field in ('metadata_sha256', 'source_signature'):
        value = review.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            return 'review_invalid', '训练审核缺少有效来源绑定'
    if (review.get('metadata_sha256') != recording.get('metadata_sha256')
            or review.get('source_signature') != recording.get('source_signature')
            or review.get('episode_id') != recording.get('run_id', recording.get('name'))):
        return 'source_changed', '来源已变化，待重新审核'
    if review.get('approved') is not True or review.get('split') not in ('train', 'validation', 'exclude'):
        return 'review_invalid', '审核尚未明确批准用途'
    if not isinstance(review.get('reviewed_at'), str) or not review['reviewed_at']:
        return 'review_invalid', '训练审核缺少时间'
    if review.get('outcome') not in OUTCOMES:
        return 'review_invalid', '训练审核结果未识别'
    if review.get('confirmation_kind', 'none') not in ('none', 'manual_confirmation', 'live_success', 'legacy_save', 'reviewed_failure'):
        return 'review_invalid', '训练审核确认方式未识别'
    for field in ('notes_summary', 'excluded_summary', 'source_label', 'confirmation_label'):
        value = review.get(field, '')
        if not isinstance(value, str) or len(value) > 4000:
            return 'review_invalid', '训练审核文字字段无效'
    refs = review.get('evidence_refs', [])
    if not isinstance(refs, list) or len(refs) > 64 or any(not isinstance(x, str) or len(x) > 2048 for x in refs):
        return 'review_invalid', '训练审核证据列表无效'
    if review['split'] in ('train', 'validation'):
        if review['outcome'] not in ('assisted_success', 'autonomous_success', 'failure'):
            return 'review_invalid', '训练审核尚未确认局部结果'
        count, seconds = review.get('transition_count'), review.get('retained_seconds')
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0 or not _number(seconds) or seconds <= 0:
            return 'review_invalid', '训练审核缺少有效转移范围'
    for field in ('local_start_s', 'terminal_s', 'original_end_s', 'terminal_monotonic_ns'):
        value = review.get(field)
        if value is not None and (not _number(value) or value < 0):
            return 'review_invalid', '训练审核时间范围无效'
    return None


def assess(recording, manifest=None):
    """Return an API projection; ``manifest`` is accepted for catalog integration.

    The cached on-disk manifest is authoritative and its SHA must still match the
    catalog row. The optional argument never bypasses source checks.
    """
    if recording.get('capture_mode') != 'rlt':
        return None
    result = _default(recording)
    try:
        sidecar = annotations.path_for(recording)
        run = sidecar.parent
        # Recovery entries can be catalogued before the journal's final rename.
        # Only inspect file metadata here: partial events/rewards are not a
        # completed recording and must never be interpreted for admission.
        if (_file_key(run / 'journal/manifest.json') is None
                and _file_key(run / 'journal.partial/manifest.json') is not None):
            return _pending(result, 'recording_incomplete', '尚未完整封存',
                            '录制尚未完整封存，封存后自动复核；当前不批准训练')
        paths = [run / 'journal/manifest.json', run / 'journal/events.jsonl',
                 run / 'journal/control.jsonl', sidecar]
        keys = tuple(_file_key(path) for path in paths)
        data = _read_evidence(str(run), *keys)
        if keys != tuple(_file_key(path) for path in paths):
            return _pending(result, 'source_changed', '录制正在变化', '本次读取期间原件变化，等待封存后重新读取')
    except (OSError, ValueError, TypeError) as error:
        return _pending(result, 'unreadable', '证据待核查', str(error))
    if not recording.get('metadata_sha256') or data['manifest_sha256'] != recording['metadata_sha256']:
        return _pending(result, 'source_changed', '来源已变化，待重新审核', '当前manifest与目录身份不一致')
    raw = data['manifest']
    raw_id = raw.get('episode_id')
    if raw_id != recording.get('run_id', recording.get('name')):
        return _pending(result, 'source_changed', '来源已变化，待重新审核', '原件episode身份不一致')
    segments, origin = data['segments'], data['recording_start_ns']
    rlt_binding = raw.get('binding', {}).get('rlt')
    phase = rlt_binding.get('phase_control') if isinstance(rlt_binding, dict) else None
    if phase == 'f710_dpad_right_v1' and data['old_save']:
        result.update(confirmation_kind='legacy_save', confirmation_label='旧协议↑仅保存整条')
    result['local_segment_count'] = len(segments) if not data['event_error'] else None
    result['local_segments'] = [dict(segment_id=s['segment_id'], outcome=_raw_outcome(s),
        local_start_s=_seconds(s['opened'].get('started_ns'), origin),
        original_end_s=_seconds((s['closed'] or {}).get('ended_ns'), origin)) for s in segments]
    if segments:
        outcomes = {_raw_outcome(s) for s in segments}
        outcome = next(iter(outcomes)) if len(outcomes) == 1 else 'mixed'
        result.update(outcome=outcome, outcome_label=OUTCOMES[outcome],
                      local_start_s=result['local_segments'][0]['local_start_s'])
        if len(segments) == 1:
            closed = segments[0].get('closed') or {}
            terminal = closed.get('terminal_observation') or {}
            result.update(original_status=closed.get('status') or 'active',
                          original_end_s=_seconds(closed.get('ended_ns'), origin),
                          terminal_s=_seconds(terminal.get('available_monotonic_ns'), origin),
                          terminal_observation_id=terminal.get('observation_id'))
            if outcome in ('assisted_success', 'autonomous_success') and closed.get('termination_reason') == 'human_confirmed':
                result['confirmation_label'] = '原始↑已确认'
                result['confirmation_kind'] = 'live_success'
            elif outcome == 'failure':
                result['confirmation_label'] = '未确认成功而正常结束'
            elif outcome == 'interrupted':
                result['confirmation_label'] = '异常中断，待人工审核'
        result['notes_summary'] = '局部事件已记录；转移质量、终点和训练用途仍需独立审核'
    elif not data['event_error']:
        result.update(outcome='not_entered', outcome_label=OUTCOMES['not_entered'])
    count = raw.get('counts', {}).get('control')
    if keys[2] is None or keys[2][2] == 0 or count == 0:
        result.update(quality='missing_control', split='exclude', usage_label='不可训练：缺实际动作日志',
                      status_label='不可训练', notes_summary='缺少实际控制发布日志，不能用提案或反馈补造动作',
                      transition_count=0, retained_seconds=0)
        if not segments:
            result.update(outcome='unknown', outcome_label='结果未确认（缺动作日志）')
        # A source-bound human report can describe the task outcome even when
        # the actual-action stream is missing. Its only permitted admission is
        # exclusion; no success report can reconstruct missing training actions.
        excluded_review = data['document'].get('rlt_training_review')
        if (excluded_review is not None and not _review_error(excluded_review, recording)
                and excluded_review['split'] == 'exclude'):
            outcome = excluded_review['outcome']
            label = OUTCOMES[outcome]
            if excluded_review.get('confirmation_kind') == 'legacy_save' and outcome in ('assisted_success', 'autonomous_success'):
                label = '人工报告：' + label
            result.update(outcome=outcome, outcome_label=label,
                          confirmation_label=excluded_review.get('confirmation_label', result['confirmation_label']),
                          confirmation_kind=excluded_review.get('confirmation_kind', result['confirmation_kind']),
                          source_bound=True, snapshot=True,
                          source_label=excluded_review.get('source_label', '源绑定人工审核'),
                          reviewed_at=excluded_review['reviewed_at'],
                          notes_summary=excluded_review.get('notes_summary') or result['notes_summary'],
                          excluded_summary=excluded_review.get('excluded_summary') or '缺少实际动作日志，不可训练',
                          evidence_refs=copy.deepcopy(excluded_review.get('evidence_refs', [])))
        return result
    if raw.get('complete') is not True or raw.get('writer_error'):
        return _pending(result, 'recording_incomplete', '录制不完整，待审核', '原件未完整封存或writer报告错误')
    if data['event_error']:
        return _pending(result, 'unreadable', '局部事件待核查', data['event_error'])
    review = data['document'].get('rlt_training_review')
    if review is not None:
        error = _review_error(review, recording)
        if error:
            return _pending(result, error[0], error[1], error[1])
        if not segments and review['split'] != 'exclude':
            return _pending(result, 'review_invalid', '审核与原件不符', '未找到局部RLT段，不能按训练审核直接准入')
        if len(segments) > 1 and review['split'] in ('train', 'validation'):
            # The current audit snapshot describes one local task. A recording
            # may contain several; never promote one review to whole-recording
            # success or silently assign its terminal to another local task.
            result.update(outcome='mixed', outcome_label=OUTCOMES['mixed'])
            return _pending(result, 'pending_review', '含多个局部段，需逐段审核',
                            '当前训练审核未逐段绑定来源；各段结果保留，整条不直接准入')
        terminal_s = _seconds(review.get('terminal_monotonic_ns'), origin)
        if terminal_s is None:
            terminal_s = review.get('terminal_s', result['terminal_s'])
        if terminal_s is not None and len(segments) == 1:
            start_s, end_s = result['local_start_s'], result['original_end_s']
            if (start_s is not None and terminal_s < start_s) or (end_s is not None and terminal_s > end_s):
                return _pending(result, 'review_invalid', '审核终点超出局部段', '成功补标必须保留原局部段的实际执行边界')
        split = review['split']
        result.update(quality='reviewed', source_bound=True, snapshot=True,
                      split=split, outcome=review['outcome'], outcome_label=OUTCOMES[review['outcome']],
                      usage_label={'train': '训练集（审核快照）', 'validation': '验证留出（不入梯度）', 'exclude': '审核排除'}[split],
                      status_label='已审核', confirmation_label=review.get('confirmation_label', result['confirmation_label']),
                      confirmation_kind=review.get('confirmation_kind', result['confirmation_kind']),
                      transition_count=review.get('transition_count', 0 if split == 'exclude' else None),
                      retained_seconds=review.get('retained_seconds', 0 if split == 'exclude' else None),
                      notes_summary=review.get('notes_summary', ''), source_label=review.get('source_label', '源绑定审核'),
                      reviewed_at=review['reviewed_at'], terminal_s=terminal_s,
                      terminal_observation_id=review.get('terminal_observation_id', result['terminal_observation_id']),
                      excluded_summary=review.get('excluded_summary', ''),
                      evidence_refs=copy.deepcopy(review.get('evidence_refs', [])))
        return result
    if not segments:
        result.update(quality='no_local_segment', split='exclude', usage_label='未产生局部RLT转移',
                      status_label='无局部RLT', transition_count=0, retained_seconds=0,
                      notes_summary='本条使用RLT启动入口，但没有进入局部RLT；不作为RLT失败奖励')
    return result


def training_review_sha256(review):
    """Canonical digest for an explicit compare-and-swap audit replacement."""
    encoded = json.dumps(review, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def save_training_review(recording, review, *, expected_existing_sha256=None):
    """Explicit audit import only; does not alter notes, raw rewards, or learners."""
    if recording.get('capture_mode') != 'rlt':
        raise ValueError('仅支持RLT来源的局部训练审核')
    error = _review_error(review, recording)
    if error:
        raise ValueError(error[1])
    with annotations.lock(recording):
        path = annotations.path_for(recording)
        manifest, sha = _read_json(path.parent / 'journal/manifest.json')
        if sha != recording.get('metadata_sha256') or manifest.get('episode_id') != review.get('episode_id'):
            raise ValueError('原件已变化，不能写入过期训练审核')
        if path.exists():
            document = annotations.read_document(path)
        else:
            document = dict(schema=annotations.SCHEMA, recording_path=str(path.parent),
                            relocation_key=annotations.relocation_key(recording), review={}, review_present=False)
        if document.get('rlt_training_review') == review:
            return path
        if 'rlt_training_review' in document:
            current_sha = training_review_sha256(document['rlt_training_review'])
            if expected_existing_sha256 != current_sha:
                raise ValueError('已有RLT训练审核已变化或未提供版本，刷新后才能替换')
        document['rlt_training_review'] = copy.deepcopy(review)
        atomic_json(path, document)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return path
