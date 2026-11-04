"""Persistent logical folders. Never writes, renames or moves raw recordings."""
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import re
import threading
import unicodedata
import uuid

from core import atomic_json


class OrganizationConflict(ValueError):
    def __init__(self, code, message, organization=None):
        super().__init__(message)
        self.code, self.organization = code, organization


def source_parent(row):
    if row.get('library_folder'):
        return canonical_folder(row['library_folder'])
    source = row.get('source')
    if source == 'hil_demonstration':
        return 'HIL/人工示教'
    if source == 'hil_policy_hil':
        return 'HIL/DAgger'
    if source in ('pico_capture', 'lerobot'):
        return 'LXL/独立上肢录制'
    if source == 'lxl':
        return '/'.join(['LXL'] + (row.get('relative_path') or row['name']).split('/')[:-1])
    return '其他录制'


def anchors(episodes):
    result = {'', 'HIL', 'HIL/RLT', 'HIL/DAgger', 'HIL/人工示教', 'LXL', 'LXL/独立上肢录制', '其他录制'}
    for row in episodes:
        if row.get('source') == 'unified_dataset':
            continue
        parts = source_parent(row).split('/')
        result.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    return result


def canonical_folder(value):
    """Compatibility for saved source anchors; custom folder IDs stay stable."""
    for old, new in (('DAgger/人工示教', 'HIL/人工示教'), ('DAgger/策略 HIL', 'HIL/DAgger'), ('真机rollout', '真机Rollout'), ('DAgger', 'HIL')):
        if value == old or value.startswith(old + '/'):
            return new + value[len(old):]
    return value


class DataOrganization:
    def __init__(self, runtime):
        self.path = Path(runtime).resolve() / 'data_organization.json'
        self.lock = threading.RLock()

    @staticmethod
    def name(value):
        if not isinstance(value, str):
            raise ValueError('文件夹名称须为文本')
        value = unicodedata.normalize('NFC', value.strip())
        if not 1 <= len(value) <= 80 or value in ('.', '..') or any(c in '/\\' or unicodedata.category(c).startswith('C') for c in value):
            raise ValueError('名称须为 1–80 字符，不含斜杠或控制字符')
        return value

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {'schema': 1, 'folders': [], 'memberships': {}}
        if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('folders'), list) or not isinstance(data.get('memberships'), dict):
            raise ValueError('版本目录文件损坏，请恢复目录元数据')
        folders = {}
        for row in data['folders']:
            if not isinstance(row, dict) or not re.fullmatch('@[a-f0-9]{32}', str(row.get('id', ''))) or not isinstance(row.get('parent'), str) or self.name(row.get('name')) != row['name'] or row['id'] in folders:
                raise ValueError('版本目录条目无效')
            folders[row['id']] = row
        siblings = set()
        for row in folders.values():
            key = (row['parent'], row['name'].casefold())
            if key in siblings:
                raise ValueError('版本目录名称重复')
            siblings.add(key)
            seen, parent = {row['id']}, row['parent']
            while parent.startswith('@'):
                if parent in seen or parent not in folders or len(seen) > 32:
                    raise ValueError('版本目录层级无效')
                seen.add(parent)
                parent = folders[parent]['parent']
        for key, row in data['memberships'].items():
            if not re.fullmatch('[a-f0-9]{24}', key) or not isinstance(row, dict) or not isinstance(row.get('folder'), str) or not isinstance(row.get('source_signature'), str) or (row['folder'].startswith('@') and row['folder'] not in folders):
                raise ValueError('版本目录录制关联无效')
        data.pop('revision', None)
        data['revision'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return data

    def command(self, body, catalog):
        # Thread and process serialization protects compare-and-swap across workers.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.read()
            if body.get('expected_revision') != current['revision']:
                raise OrganizationConflict('organization_conflict', '目录已被其他操作更新，请刷新后重试', current)
            data = copy.deepcopy(current)
            for f in data['folders']:
                f['parent'] = canonical_folder(f['parent'])
            for m in data['memberships'].values():
                m['folder'] = canonical_folder(m['folder'])
            body = dict(body)
            for key in ('parent', 'target'):
                if isinstance(body.get(key), str):
                    body[key] = canonical_folder(body[key])
            folders = {f['id']: f for f in data['folders']}
            snapshot = catalog()
            episodes = {e['id']: e for e in snapshot['episodes'] if e.get('source') != 'unified_dataset'}
            allowed = anchors(episodes.values())
            # Keep existing anchors during partial/unavailable raw catalog reads.
            allowed.update(f['parent'] for f in folders.values() if not f['parent'].startswith('@'))
            allowed.update(m['folder'] for m in data['memberships'].values() if not m['folder'].startswith('@'))
            allowed.update(folders)
            op = body.get('operation')
            if op in ('create', 'rename'):
                name = self.name(body.get('name'))
                if op == 'create':
                    if len(folders) >= 2000:
                        raise ValueError('版本文件夹已达 2000 个')
                    row = {'id': '@' + uuid.uuid4().hex, 'parent': body.get('parent'), 'name': name}
                    data['folders'].append(row)
                else:
                    row = folders.get(body.get('folder_id'))
                    if not row:
                        raise ValueError('只能重命名自建文件夹')
                    row['name'] = name
                target = row['parent']
            elif op == 'move':
                target = body.get('target')
                folder_id = body.get('folder_id')
                selected = body.get('episodes', [])
                if folder_id:
                    if selected or folder_id not in folders:
                        raise ValueError('移动文件夹参数无效')
                    row = folders[folder_id]
                    row['parent'] = target
                else:
                    if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
                        raise ValueError('每次选择 1–100 条录制')
                    for item in selected:
                        if not isinstance(item, dict):
                            raise ValueError('录制参数无效')
                        episode = episodes.get(item.get('id'))
                        if not episode or not item.get('source_signature') or episode.get('source_signature') != item['source_signature']:
                            raise OrganizationConflict('source_changed', '录制来源已变化或暂不可用，请重新读取并选择', current)
                        data['memberships'][episode['id']] = {'folder': target, 'source_signature': item['source_signature']}
                    row = None
            else:
                raise ValueError('未知目录操作')
            if not isinstance(target, str) or target not in allowed:
                raise ValueError('目标文件夹不存在')
            if row:
                if any(f['id'] != row['id'] and f['parent'] == target and f['name'].casefold() == row['name'].casefold() for f in data['folders']):
                    raise ValueError('此位置已有同名文件夹')
                if not target.startswith('@') and '/'.join(filter(None, [target, row['name']])) in allowed:
                    raise ValueError('此位置已有同名来源目录')
                graph = {f['id']: f for f in data['folders']}
                for f in graph.values():
                    seen, parent = {f['id']}, f['parent']
                    while parent.startswith('@'):
                        if parent in seen:
                            raise ValueError('不能移入自身或子文件夹')
                        if len(seen) >= 32:
                            raise ValueError('文件夹最多支持 32 层')
                        seen.add(parent)
                        parent = graph[parent]['parent']
            data.pop('revision', None)
            atomic_json(self.path, data)
            return self.read()
