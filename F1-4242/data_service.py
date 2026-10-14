"""LXL rosbag catalog with source-bound, independent review sidecars.

Uses the meaxiao review boundary (raw dataset immutable, source identity and
annotations outside it), adapted to the existing MCAP format. No collectors,
UDP commands, upload jobs, bag readers, or dataset conversion are started here.
Explicit selected deletion is isolated in data_delete.py.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from datetime import datetime, timezone
from core import atomic_json, utc


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class DataService:
    def __init__(self, root, runtime, web_root, proc_root='/proc', extra_catalog=None):
        self.root = Path(root).absolute()
        self.review_root = Path(runtime).resolve() / 'data_reviews'
        if self.review_root.is_relative_to(self.root.resolve()):
            raise ValueError('审核缓存不能位于原始数据目录')
        self.web_root = Path(web_root).resolve()
        self.proc_root = Path(proc_root)
        self.extra_catalog = extra_catalog
        self.preview_root = Path(runtime).resolve() / 'data_previews'
        self.lock = threading.RLock()
        self.cached = None
        self.cache_at = 0

    def _confined(self, path):
        path = Path(path)
        try:
            if not path.is_relative_to(self.root):
                return False
            parts = path.relative_to(self.root).parts
            current = self.root
            if current.is_symlink():
                return False
            for part in parts:
                current = current / part
                if current.is_symlink():
                    return False
            return path.resolve().is_relative_to(self.root.resolve())
        except OSError:
            return False

    def _walk(self, start, depth=4):
        """Bounded file inventory: no symlinks, metadata only, no raw bag reads."""
        pending = [(start, 0)]
        visited = 0
        while pending:
            directory, level = pending.pop()
            if not self._confined(directory):
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > 20_000:
                            raise ValueError('目录条目过多，需缩小数据根目录')
                        if entry.is_symlink() or entry.name.startswith(".f1-delete-"):
                            continue
                        p = Path(entry.path)
                        if entry.is_file(follow_symlinks=False):
                            yield p, entry.stat(follow_symlinks=False)
                        elif entry.is_dir(follow_symlinks=False) and level < depth:
                            pending.append((p, level + 1))
            except (FileNotFoundError, PermissionError):
                continue

    def _small_file(self, path):
        if not self._confined(path):
            raise ValueError('数据路径越界或包含符号链接')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > 1_000_000:
                raise ValueError('metadata 大小或类型无效')
            return handle.read(1_000_001)

    def _metadata(self, directory):
        result = dict(metadata_status='missing', metadata_sha256=None, started_at=None,
                      duration_s=None, message_count=None, topics=[])
        p = directory / 'metadata.yaml'
        if not p.is_file() or not self._confined(p):
            return result
        try:
            raw = self._small_file(p)
            result['metadata_sha256'] = hashlib.sha256(raw).hexdigest()
            import yaml
            info = yaml.safe_load(raw)['rosbag2_bagfile_information']
            result.update(metadata_status='parsed',
                          started_at=iso(int(info['starting_time']['nanoseconds_since_epoch']) / 1e9),
                          duration_s=int(info['duration']['nanoseconds']) / 1e9,
                          message_count=int(info['message_count']),
                          topics=[{'name': str(row['topic_metadata']['name']),
                                   'type': str(row['topic_metadata']['type']),
                                   'message_count': int(row['message_count'])}
                                  for row in info.get('topics_with_message_count', [])])
        except Exception as exc:
            result.update(metadata_status='unavailable', metadata_error=str(exc)[:200])
        return result

    def _preview(self, directory, metadata_sha):
        if not metadata_sha:
            return None
        for p in sorted((self.web_root / 'previews').glob('*/preview.json')):
            try:
                if p.is_symlink() or p.stat().st_size > 1_000_000:
                    continue
                saved = json.loads(p.read_text())
                if saved.get('selected_episode') != directory.name:
                    continue
                if any(row.get('source_path') == str(directory) and
                       row.get('metadata_sha256') == metadata_sha for row in saved.get('episodes', [])):
                    return '/' + str(p.parent.relative_to(self.web_root)) + '/preview.html'
            except (OSError, ValueError, TypeError):
                pass
        return None

    def _review(self, eid, signature):
        pending = {'status': 'pending', 'notes': '', 'updated_at': None, 'stale': False}
        p = self.review_root / (eid + '.json')
        try:
            if p.is_symlink() or not p.exists():
                return pending
            row = json.loads(p.read_text())
            if row.get('source_signature') != signature:
                return dict(pending, stale=True, previous_review=row)
            return row
        except (OSError, ValueError):
            return pending

    def _episode(self, directory):
        files = []
        signature_files = []
        for p, status in self._walk(directory):
            relative = str(p.relative_to(directory))
            files.append({'name': p.name, 'relative_path': relative, 'size_bytes': status.st_size,
                          'modified_at': iso(status.st_mtime)})
            signature_files.append([relative, status.st_size, status.st_mtime_ns])
        if not files:
            return None
        files.sort(key=lambda row: row['relative_path'])
        rel = str(directory.relative_to(self.root))
        eid = hashlib.sha256(rel.encode()).hexdigest()[:24]
        meta = self._metadata(directory)
        signature = hashlib.sha256(json.dumps([str(self.root), rel, sorted(signature_files),
                                               meta['metadata_sha256']], sort_keys=True).encode()).hexdigest()
        row = dict(id=eid, name=directory.name, path=str(directory), relative_path=rel, source='lxl',
                    format='rosbag2_mcap' if any(row['name'].endswith('.mcap') for row in files) else 'rosbag2',
                    size_bytes=sum(row['size_bytes'] for row in files), file_count=len(files),
                    modified_at=max(row['modified_at'] for row in files), files=files,
                    source_signature=signature, review=self._review(eid, signature),
                    preview_url=self._preview(directory, meta['metadata_sha256']), **meta)
        preview = self.preview_root / eid / 'manifest.json'
        try:
            if preview.is_file() and not preview.is_symlink() and preview.stat().st_size < 100_000:
                saved = json.loads(preview.read_text())
                if saved.get('source_signature') == signature:
                    row['videos'] = saved.get('videos', [])
                    row['analytics_url'] = '/api/data/analytics/' + eid
                    row['preview_notes'] = saved.get('notes', [])
        except (OSError, ValueError):
            pass
        return row

    def recording(self):
        processes = []
        proc_readable = self.proc_root.is_dir()
        try:
            for p in self.proc_root.iterdir():
                if not p.name.isdigit():
                    continue
                try:
                    args = [part.decode(errors='replace') for part in (p/'cmdline').read_bytes().split(b'\0') if part]
                except OSError:
                    continue
                # LXL launches `ros2 bag record -o <pico_data/...>` as a child.
                # A bridge process alone does not prove that a recording is active.
                command = ' '.join(args)
                if 'bag' in args and 'record' in args and any(arg.startswith(str(self.root) + '/') for arg in args):
                    processes.append({'pid': int(p.name), 'kind': 'ros2_bag_record'})
        except OSError:
            proc_readable = False
        return {'state': 'observed_active' if processes else ('no_process_observed' if proc_readable else 'unknown'),
                'control_enabled': False, 'processes': processes, 'checked_at': utc(),
                'message': '现有 LXL 录制进程已观察到；不代表数据完整性已验收' if processes else
                           '未观察到此目录的 ros2 bag record 进程；录制、保存和上传沿用原流程',
                'integration': 'existing_lxl_read_only',
                'reason': 'meaxiao LeRobot 录制器尚未迁入 F1-09；采集格式、落盘及上传配置保持现状'}

    def catalog(self, refresh=False):
        with self.lock:
            if not refresh and self.cached is not None and time.monotonic() - self.cache_at < 3:
                return copy.deepcopy(self.cached)
            available = self.root.is_dir() and self._confined(self.root)
            episodes = []
            error = None
            if available:
                try:
                    candidates = {p.parent for p, _ in self._walk(self.root)
                                  if p.name == 'metadata.yaml' or p.suffix in ('.mcap', '.db3')}
                    for directory in sorted(candidates):
                        row = self._episode(directory)
                        if row:
                            episodes.append(row)
                except (OSError, ValueError) as exc:
                    error = str(exc)
            if self.extra_catalog:
                try:
                    extra = self.extra_catalog()
                    for original in extra:
                        row = dict(original)
                        row['review'] = self._review(row['id'], row['source_signature'])
                        episodes.append(row)
                except Exception as exc:
                    error = '独立录制目录暂不可用，LXL目录仍可查看：' + str(exc)
            self.cached = {'root': str(self.root), 'available': available, 'episodes': episodes,
                           'summary': {'episode_count': len(episodes),
                                       'total_bytes': sum(row['size_bytes'] for row in episodes)},
                           'recording': self.recording(), 'checked_at': utc(), 'error': error}
            self.cache_at = time.monotonic()
            return copy.deepcopy(self.cached)

    def episode(self, eid, refresh=False):
        if not isinstance(eid, str) or not re.fullmatch('[a-f0-9]{24}', eid):
            raise ValueError('数据条目 ID 无效')
        for row in self.catalog(refresh)['episodes']:
            if row['id'] == eid:
                return row
        raise ValueError('数据条目不存在')

    def save_review(self, eid, status, notes, reviewer, expected_source_signature=None):
        if status not in ('pending', 'approved', 'rejected'):
            raise ValueError('审核状态须为 pending、approved 或 rejected')
        if not isinstance(notes, str) or len(notes) > 4000:
            raise ValueError('审核备注不得超过 4000 字符')
        with self.lock:
            row = self.episode(eid, refresh=True)
            if not isinstance(expected_source_signature, str) or expected_source_signature != row['source_signature']:
                raise ValueError('来源已变化或缺少来源签名，请刷新数据并重新审核')
            review = {'episode_id': eid, 'status': status, 'notes': notes, 'updated_at': utc(),
                      'reviewer': reviewer, 'source_relative_path': row['relative_path'],
                      'source_signature': row['source_signature'],
                      'metadata_sha256': row['metadata_sha256'], 'stale': False,
                      'scope': '人工内容审核；不等于监督合同、训练质量或完整性验收'}
            # Only our runtime receives writes, never the source bag directory.
            self.review_root.mkdir(parents=True, exist_ok=True)
            if self.review_root.is_symlink() or not self.review_root.resolve().is_relative_to(self.review_root.parent):
                raise ValueError('审核目录路径无效')
            atomic_json(self.review_root / (eid + '.json'), review)
            self.cache_at = 0
            return review
