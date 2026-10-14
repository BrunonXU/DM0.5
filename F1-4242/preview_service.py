"""Confined read-only media access for derived LXL previews and LeRobot data."""
import json,re
from pathlib import Path
from recording_service import confined_file

class PreviewService:
    def __init__(self,runtime,data,recording):
        self.root=Path(runtime).resolve()/'data_previews';self.data=data;self.recording=recording
    def _episode(self,eid):
        if not re.fullmatch('[0-9a-f]{24}',eid):raise ValueError('数据ID无效')
        return self.data.episode(eid)
    def video(self,eid,camera):
        if camera not in ('head','left','right'):raise ValueError('相机无效')
        row=self._episode(eid)
        if row.get('source')=='lerobot':return self.recording.video(eid,camera)
        if not any(v.get('camera')==camera for v in row.get('videos',[])):raise ValueError('尚未生成这一路历史视频')
        return confined_file(self.root,eid+'/'+camera+'.mp4')
    def analytics(self,eid):
        row=self._episode(eid)
        if row.get('source')=='lerobot':return self.recording.analytics(eid)
        if not row.get('analytics_url'):raise ValueError('尚未生成历史曲线')
        return confined_file(self.root,eid+'/analytics.json')

def byte_range(header,size):
    if not header:return 0,max(0,size-1),False
    match=re.fullmatch(r'bytes=(\d*)-(\d*)',header.strip())
    if not match or not any(match.groups()):raise ValueError('invalid range')
    a,b=match.groups()
    if not a:
        count=int(b)
        if count<=0:raise ValueError('invalid suffix')
        start=max(0,size-count);end=size-1
    else:start=int(a);end=min(int(b),size-1) if b else size-1
    if start>=size or start>end:raise ValueError('range outside resource')
    return start,end,True
