"""Small per-episode report index. Never scans captures or controls the robot."""
import json
import os
from pathlib import Path
import re
import time
import uuid


def _index(root, episode_id):
    if not isinstance(episode_id, str) or not re.fullmatch(r'[0-9a-f]{24}', episode_id):
        raise ValueError('录制编号无效')
    root = Path(root).absolute()
    folder = root/'latest'
    if any(p.is_symlink() for p in (folder, *folder.parents)):
        raise ValueError('报告索引路径无效')
    path = folder/(episode_id+'.json')
    if path.is_symlink():
        raise ValueError('报告索引不能为符号链接')
    return path


def record_latest(report_path, request, result):
    report_path = Path(report_path)
    path = _index(report_path.parent, request.get('episode_id'))
    report_id = request.get('report_id', '')
    if not re.fullmatch(r'[0-9a-f]{32}', report_id) or report_path.name != report_id+'.json':
        raise ValueError('报告编号无效')
    row = {key:request.get(key) for key in ('episode_id','report_id','source_signature','created_at','start_s','end_s')}
    row.update(finished_at=time.time(), status=result.get('status'))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x') as stream:
            json.dump(row, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recent(root, episode_id):
    path = _index(root, episode_id)
    if not path.exists():
        return []
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError('报告索引无效')
    row = json.loads(path.read_text())
    if not isinstance(row,dict) or row.get('episode_id') != episode_id or not re.fullmatch(r'[0-9a-f]{32}',str(row.get('report_id',''))):
        raise ValueError('报告索引不匹配')
    report = Path(root)/(row['report_id']+'.json')
    if report.is_symlink() or not report.is_file():
        raise ValueError('最近报告不可读取')
    return [row]
