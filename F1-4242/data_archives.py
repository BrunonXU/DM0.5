"""Read a bounded, local archive index; never contact or operate on archived data.

Archive entries describe the last verified copy on another machine. They are not
local recordings and must stay outside episode lookup, storage totals and Replay.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat


SCHEMA = 'f1_data_archives_v1'
MAX_BYTES = 128 * 1024
MAX_RECORDS = 256
MODES = {
    'dagger': ('DAgger', 'HIL/DAgger'),
    'demonstration': ('人工示教', 'HIL/人工示教'),
    'rlt': ('RLT', 'HIL/RLT'),
    'rollout': ('真机Rollout', '真机Rollout'),
    'lxl': ('LXL', 'LXL'),
}
FIELDS = {'id', 'name', 'capture_mode', 'started_at', 'storage_label',
          'availability', 'source_deleted', 'purpose_label', 'notes',
          'source_identity', 'integrity_checked_at', 'verified', 'deleted_at'}


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or
            (not empty and not value.strip()) or
            any(ord(c) < 32 and c not in '\n\t' for c in value)):
        raise ValueError('归档索引文本无效')
    return value


def _timestamp(value):
    _text(value, 48)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('归档索引时间无效') from None
    if parsed.utcoffset() is None:
        raise ValueError('归档索引时间缺少时区')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('归档索引字段重复')
        result[key] = value
    return result


def _record(row):
    if not isinstance(row, dict) or set(row) - FIELDS or FIELDS - {'deleted_at'} - set(row):
        raise ValueError('归档索引条目字段无效')
    eid = row['id']
    if not isinstance(eid, str) or not re.fullmatch('[a-f0-9]{24}', eid):
        raise ValueError('归档索引身份无效')
    mode = row['capture_mode']
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('归档索引采集模式无效')
    if row['availability'] != 'archived' or type(row['source_deleted']) is not bool or type(row['verified']) is not bool:
        raise ValueError('归档索引状态无效')
    identity = row['source_identity']
    if (not isinstance(identity, dict) or set(identity) != {'run_id', 'manifest_sha256'} or
            not isinstance(identity['run_id'], str) or not re.fullmatch('[A-Za-z0-9_-]{1,120}', identity['run_id']) or
            not isinstance(identity['manifest_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', identity['manifest_sha256'])):
        raise ValueError('归档索引来源绑定无效')
    if row['verified'] or row['integrity_checked_at'] is not None:
        _timestamp(row['integrity_checked_at'])
    if 'deleted_at' in row:
        if not row['source_deleted']:
            raise ValueError('归档索引删除状态不一致')
        _timestamp(row['deleted_at'])
    label, folder = MODES[mode]
    result = {
        'id': eid, 'name': _text(row['name'], 160),
        'capture_mode': mode, 'capture_label': label, 'library_folder': folder,
        'started_at': _timestamp(row['started_at']),
        'storage_label': _text(row['storage_label'], 80),
        'availability': 'archived', 'source_deleted': row['source_deleted'],
        'purpose_label': _text(row['purpose_label'], 120),
        'notes': _text(row['notes'], 4000, empty=True),
        'source_identity': dict(identity), 'integrity_checked_at': row['integrity_checked_at'],
        'verified': row['verified'], 'read_only': True,
        'label': '服务器归档 · 只读', 'display_timezone': 'Asia/Shanghai',
        'availability_label': ('机器人已删除 · ' if row['source_deleted'] else '') +
                              ('服务器镜像上次核验完整' if row['verified'] else '服务器镜像尚未核验'),
    }
    if 'deleted_at' in row:
        result['deleted_at'] = row['deleted_at']
    return result


class DataArchives:
    def __init__(self, runtime):
        self.path = Path(runtime).resolve() / 'data_archives.json'

    def read(self):
        """Missing index is empty; callers isolate other errors from the catalog."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError('归档索引文件无效或过大')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('归档索引文件过大')
        try:
            document = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ValueError('归档索引无法解析') from None
        if (not isinstance(document, dict) or set(document) != {'schema', 'records'} or
                document['schema'] != SCHEMA or not isinstance(document['records'], list) or
                len(document['records']) > MAX_RECORDS):
            raise ValueError('归档索引格式无效')
        rows = [_record(row) for row in document['records']]
        if len({row['id'] for row in rows}) != len(rows):
            raise ValueError('归档索引身份重复')
        return sorted(rows, key=lambda row: datetime.fromisoformat(row['started_at'].replace('Z', '+00:00')), reverse=True)
