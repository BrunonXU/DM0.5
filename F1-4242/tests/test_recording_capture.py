"""Real ffmpeg/Parquet transaction using synthetic images only; never imports ROS."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from recording.upstream.lerobot_v3 import CAMERA_KEYS, FEATURE_NAMES, LeRobotV3Dataset
from recording.upstream.alignment_archive import archive_frame, read_extras, validate_coverage


def capture_class():
    source = Path(__file__).resolve().parents[1]/'recording/upstream/lerobot_recorder.py'
    tree = ast.parse(source.read_text())
    cls = next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='EpisodeCapture')
    env = dict(np=np,Path=Path,tempfile=tempfile,queue=queue,subprocess=subprocess,threading=threading,
               time=time,json=json,os=os,shutil=shutil,CAMERA_KEYS=CAMERA_KEYS,
               archive_frame=archive_frame,read_extras=read_extras,validate_coverage=validate_coverage)
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(source),'exec'),env)
    return env['EpisodeCapture']


@pytest.mark.skipif(not shutil.which('ffmpeg') or not importlib.util.find_spec('pyarrow'),reason='ffmpeg and private pyarrow env required')
def test_synthetic_three_camera_capture_commit_and_repeat(tmp_path):
    jpeg = subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=64x48','-frames:v','1','-f','image2pipe','-vcodec','mjpeg','-threads','1','pipe:1'],check=True,capture_output=True).stdout
    config=json.loads((Path(__file__).resolve().parents[1]/'recording/config.json').read_text())
    capture=capture_class()(tmp_path,config,{key:{'width':64,'height':48} for key in CAMERA_KEYS})
    start=1_000_000_000_000
    step=int(1e9/config['fps'])
    for frame in range(12):
        stamp=start+frame*step
        age=[1_000_000]*14+[frame*step,frame*step]
        capture.add(np.arange(16,dtype=np.float32),np.arange(16,dtype=np.float32)+1,stamp,
                    {key:(jpeg,stamp) for key in CAMERA_KEYS},
                    {'capture_monotonic_ns':stamp,'camera_integrity_timestamp_ns':[stamp]*3,
                     'camera_arrival_timestamp_ns':[stamp+1_000_000]*3,'state_max_age_ns':1_000_000,
                     'action_max_age_ns':1_000_000,'action_source_timestamp_ns':[stamp-v for v in age],
                     'action_source_age_ns':age,'action_held':[False]*14+[frame>0,frame>0],
                     'gripper_writer_gid':['01'*24,'02'*24],'gripper_source_epoch':[1,1]})
    arrays,videos,timing=capture.stop()
    assert timing['training_ready'] is True
    assert arrays['state'].shape==(12,16)
    assert all(row['frames']==12 for row in videos.values())
    original=capture.path
    dataset=LeRobotV3Dataset(tmp_path,config['fps'],config['task'],int(config['camera_max_frame_gap_s']*1e9))
    first,path=dataset.commit(original,arrays,videos)
    again,path_again=dataset.commit(original,arrays,videos)
    assert first==again==0 and path==path_again
    import pyarrow.parquet as pq
    table=pq.read_table(path)
    assert table.num_rows==12
    assert table['action'].to_pylist()[0]==list(range(1,17))
    assert table['source.action_held'].to_pylist()[-1][-2:]==[True,True]
    assert table['source.action_age_ns'].to_pylist()[-1][-1]==11*step
    assert (tmp_path/'source/episode-000000/samples.jsonl').exists()
    info=json.loads((tmp_path/'meta/info.json').read_text())
    assert info['total_episodes']==1 and info['features']['action']['shape']==[16]
    assert info['training_accepted'] is False


def test_encoder_partial_start_is_cleaned_up(tmp_path,monkeypatch):
    closed=[]
    class Pipe:
        def close(self):closed.append('closed')
    class Process:
        stdin=Pipe();stderr=Pipe()
        def terminate(self):closed.append('terminated')
        def wait(self,timeout=None):return 0
    cls=capture_class()
    calls=[]
    def spawn(*args):
        calls.append(1)
        if len(calls)>1:raise OSError('synthetic second encoder startup failure')
        return Process()
    monkeypatch.setattr(cls,'_open_encoder',spawn)
    config=json.loads((Path(__file__).resolve().parents[1]/'recording/config.json').read_text())
    with pytest.raises(OSError):cls(tmp_path,config,{key:{'width':64,'height':48} for key in CAMERA_KEYS})
    assert 'terminated' in closed and closed.count('closed')==2
