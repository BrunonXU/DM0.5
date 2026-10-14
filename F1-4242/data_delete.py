"""Explicit, authenticated deletion of selected local recordings, never arbitrary paths.

Preflight the whole selection before touching sources. Stage each source by atomic
rename, audit before mutation, and report cleanup failures rather than success.
Exported datasets / recovery copies remain independent artifacts.
"""
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from core import atomic_json
from hil_data import roots as hil_roots
from recording.unified.jobs import active_capture

SUPPORTED = {'lxl', 'hil_demonstration', 'hil_policy_hil', 'pico_capture'}


def safe_path(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('删除路径包含符号链接或越界')
    return path


@contextmanager
def file_lock(path):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('采集、控制或数据处理正在运行，请结束后再删除') from None
        yield
    finally:
        os.close(fd)


def inventory(path):
    """Bound traversal; internal links are unlinked, never followed."""
    path = safe_path(path)
    st = path.stat()
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError('录制目录不存在')
    items = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            p = Path(base)/name
            s = p.lstat();items += 1
            if items > 25000:
                raise ValueError('目录过大，请使用离线数据管理')
            # os.walk(followlinks=False) never descends through directory links.
            # fd-safe rmtree unlinks them (including broken/outside links) without
            # touching the target. Roots/ancestors must still pass safe_path.
            if stat.S_ISLNK(s.st_mode):continue
            if not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)) or s.st_dev != st.st_dev or p.is_mount():
                raise ValueError('目录包含挂载点或特殊文件，拒绝删除：'+str(p.relative_to(path)))
    return st.st_dev, st.st_ino


class DataDeletion:
    def __init__(self, app, *, capture_active=active_capture):
        self.app = app
        self.capture_active = capture_active

    def _source(self, row, catalog):
        a = self.app
        if row.get('source') not in SUPPORTED:
            raise ValueError('此格式暂不支持独立删除（共享训练数据集需离线管理）')
        path = safe_path(row['path'])
        if row['source'] == 'lxl':
            allowed = [a.data.root]
        elif row['source'] == 'pico_capture':
            allowed = a.capture_data.roots()
        else:
            allowed = hil_roots(a.hil)
        if not any(path != safe_path(root) and path.is_relative_to(safe_path(root)) for root in allowed):
            raise ValueError('录制不在允许删除的数据目录内')
        # Never delete a group containing a second independently catalogued take.
        if any(Path(r['path']) != path and Path(r['path']).is_relative_to(path) for r in catalog):
            raise ValueError('此目录包含其他录制，请进入目录逐条选择')
        return path

    def _derived(self, row):
        a = self.app; eid = row['id']
        paths = [a.data.preview_root/eid, a.data.review_root/(eid+'.json')]
        jobs = []
        if row['source'].startswith('hil_'):
            root = safe_path(a.hil_media.root())
            for p in (root/'video-jobs').glob('*.json'):
                safe_path(p)
                if p.stat().st_size > 1_000_000:raise ValueError('视频任务记录过大')
                j = json.loads(p.read_text())
                if j.get('run_id') != row.get('run_id'):continue
                if not re.fullmatch('[a-f0-9]{64}', p.stem) or j.get('id') != p.stem:
                    raise ValueError('视频任务身份异常')
                if j.get('lease_until', 0) > time.time():
                    raise ValueError('这条录制的视频正在生成或传输，请完成后再删除')
                paths += [root/'videos'/p.stem, p]
                jobs.append(p.stem)
        for p in (a.runtime/'conversion-jobs').glob('*.json'):
            safe_path(p)
            if p.stat().st_size > 1_000_000:raise ValueError('转换任务记录过大')
            j = json.loads(p.read_text())
            if j.get('source') == row['path']:
                paths.append(p)
        for p in paths:
            safe_path(p)
            if p.is_dir():inventory(p)
            elif p.exists() and not p.is_file():raise ValueError('派生文件类型异常')
        return paths, jobs

    def delete(self, body, owner):
        selection = body.get('episodes')
        if body.get('confirm') is not True or not isinstance(selection, list) or not 1 <= len(selection) <= 100:
            raise ValueError('请明确确认删除 1–100 条所选录制')
        ids = []
        for s in selection:
            if not isinstance(s, dict) or not isinstance(s.get('id'), str) or not re.fullmatch('[a-f0-9]{24}', s['id']):
                raise ValueError('录制 ID 无效')
            ids.append(s['id'])
        if len(ids) != len(set(ids)):raise ValueError('重复选择录制')
        a = self.app
        # Same order as media discovery: media -> catalog. No output/control lease
        # is acquired; the OS mutex only excludes concurrent capture startup.
        with ExitStack() as stack:
            stack.enter_context(a.op_lock)
            stack.enter_context(a.recording.lock)
            stack.enter_context(a.hil_media.lock)
            stack.enter_context(file_lock(a.lease.path))
            stack.enter_context(file_lock(a.runtime/'recorder/worker.lock'))
            stack.enter_context(file_lock(a.runtime/'conversion-jobs/worker.lock'))
            if self.capture_active() or a.hil.state()['active'] or a.actions.busy() or a.data.recording()['state'] != 'no_process_observed':
                raise ValueError('采集或控制服务正在运行，请结束后再删除；不会中断当前采集')
            stack.enter_context(a.data.lock)
            rows = a.data.catalog(refresh=True)['episodes']
            plans = []
            for selected in selection:
                matches = [r for r in rows if r['id'] == selected['id']]
                if len(matches) != 1:raise ValueError('录制不存在或身份重复，请刷新目录')
                row = matches[0]
                if selected.get('source_signature') != row['source_signature']:
                    raise ValueError('所选录制已变化，请刷新并重新选择')
                path = self._source(row, rows)
                try:
                    identity = inventory(path)
                    derived, jobs = self._derived(row)
                except ValueError as exc:
                    raise ValueError(row['name']+'：'+str(exc)) from exc
                plans.append((row, path, identity, derived, jobs))
            # Durable intent also identifies leftover staging directories on IO failure.
            tid = uuid.uuid4().hex
            audit_path = safe_path(a.runtime/'data_deletions'/(tid+'.json'))
            audit = dict(id=tid, owner=owner, created_unix=time.time(), state='deleting',
                         scope='local_raw_preview_review_jobs; independent_exports_and_recovery_retained', items=[])
            for row, path, identity, derived, jobs in plans:
                audit['items'].append(dict(id=row['id'], name=row['name'], source=str(path),
                    source_signature=row['source_signature'], staged=str(path.with_name('.f1-delete-'+tid+'-'+row['id'])),
                    derived=[str(p) for p in derived], state='pending'))
            atomic_json(audit_path, audit)
            for item, (row, path, identity, derived, jobs) in zip(audit['items'], plans):
                try:
                    # Parent opened without following links; shutil uses fd-based safe
                    # traversal. No client-supplied filesystem path is ever accepted.
                    safe_path(path)
                    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                        if (st.st_dev, st.st_ino) != identity:raise ValueError('录制目录身份发生变化')
                        stage = Path(item['staged']).name
                        os.rename(path.name, stage, src_dir_fd=fd, dst_dir_fd=fd)
                        os.fsync(fd)
                        item['state'] = 'staged';atomic_json(audit_path, audit)
                        shutil.rmtree('/proc/self/fd/'+str(fd)+'/'+stage)
                    finally:os.close(fd)
                    item['state'] = 'raw_deleted';atomic_json(audit_path, audit)
                    for p in derived:
                        safe_path(p)
                        if p.is_dir():shutil.rmtree(p)
                        else:p.unlink(missing_ok=True)
                    item['state'] = 'deleted'
                except (OSError, ValueError) as exc:
                    item['error'] = str(exc)
                atomic_json(audit_path, audit)
            audit['state'] = 'complete' if all(i['state'] == 'deleted' for i in audit['items']) else 'partial'
            atomic_json(audit_path, audit)
            a.data.cached = None;a.data.cache_at = 0
            a.hil_data.index_cache = None;a.capture_data.cache = None
            a.health_at = 0
            a.events.add('data_deleted', '操作者删除本机录制', transaction=tid,
                         deleted=[i['id'] for i in audit['items'] if i['state']=='deleted'], state=audit['state'])
            return dict(ok=True, complete=audit['state']=='complete', transaction=tid, results=audit['items'])
