import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pico_f1.episode_analysis import FEATURES, analyze_episode, polynomial_derivatives, infer_stages, population_statistics
from pico_f1.lerobot_v3 import CAMERA_KEYS
from pico_f1.dashboard_server import ReviewStore, handler_for
from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
import threading


def fixture(root, episode=0, duplicate=False):
    n=90; start=1_789_300_000_000_000_000
    stamps=start+np.round(np.arange(n)/30*1e9).astype(np.int64)
    if duplicate: stamps[20]=stamps[19]
    q=np.zeros((n,27));q[:,0]=np.arange(n)/30
    table=pa.table({'observation.state':q.tolist(),'action':q.tolist(),
        'source.timestamp_ns':stamps,'source.camera_timestamp_ns':np.tile(stamps[:,None],(1,3)).tolist(),
        'source.camera_integrity_timestamp_ns':np.tile(stamps[:,None],(1,3)).tolist(),
        'source.state_max_age_ns':np.zeros(n,dtype=np.int64),'source.action_max_age_ns':np.zeros(n,dtype=np.int64),
        'timestamp':np.arange(n)/30,'frame_index':np.arange(n),'episode_index':np.full(n,episode),
        'index':np.arange(episode*n,(episode+1)*n)})
    p=root/f'data/chunk-000/file-{episode:03d}.parquet';p.parent.mkdir(parents=True,exist_ok=True);pq.write_table(table,p)
    row={'episode_index':episode,'length':n,'dataset_from_index':episode*n,'dataset_to_index':(episode+1)*n,
         'data/chunk_index':0,'data/file_index':episode}
    for key in CAMERA_KEYS:
        row.update({f'videos/{key}/chunk_index':0,f'videos/{key}/file_index':episode,
                    f'videos/{key}/from_timestamp':0.,f'videos/{key}/to_timestamp':n/30})
    meta=root/f'meta/episodes/chunk-000/file-{episode:03d}.parquet';meta.parent.mkdir(parents=True,exist_ok=True);pq.write_table(pa.Table.from_pylist([row]),meta)
    info=dict(total_episodes=episode+1,total_frames=(episode+1)*n,fps=30,
        data_path='data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet',
        video_path='videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4',
        features={'observation.state':{'names':FEATURES},'action':{'names':FEATURES}})
    (root/'meta/info.json').write_text(json.dumps(info))
    return row,info


class AnalysisTests(unittest.TestCase):
    def test_cubic_derivatives_on_actual_irregular_time(self):
        t=np.arange(100)*.033+np.sin(np.arange(100))*.001
        _,v,a,j=polynomial_derivatives(t,2*t**3-3*t**2+t+5)
        np.testing.assert_allclose(v[5:-5,0],6*t[5:-5]**2-6*t[5:-5]+1,atol=1e-8)
        np.testing.assert_allclose(a[5:-5,0],12*t[5:-5]-6,atol=1e-7)
        np.testing.assert_allclose(j[5:-5,0],12,atol=1e-6)
        self.assertTrue(np.isnan(j[:5]).all())

    def test_duplicate_timestamps_fail_without_fabricated_derivatives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r,i=fixture(root,duplicate=True);d=analyze_episode(root,r,i,False)
            self.assertTrue(any('重复' in x for x in d['summary']['issues']))
            self.assertIsNone(d['metrics'][0]['jerk_peak'])

    def test_valid_frames_and_unknown_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r,i=fixture(root);d=analyze_episode(root,r,i,False)
            self.assertEqual(d['summary']['issues'],[])
            self.assertAlmostEqual(d['summary']['effective_fps'],30,places=5)
            self.assertEqual(d['stages'],[])
            self.assertEqual(d['cameras'][0]['status'],'not_checked')

    def test_two_movement_groups_keep_turn_translate_pause_together(self):
        t=np.arange(1200)/30;base=np.zeros((len(t),3))
        base[(t>=5)&(t<10),2]=.4;base[(t>=12)&(t<16),0]=.2
        base[(t>=28)&(t<33),2]=-.4;base[(t>=34)&(t<38),0]=.2
        stages,_=infer_stages(t,base)
        self.assertEqual(len(stages),4)
        self.assertAlmostEqual(stages[1]['start'],5)
        self.assertGreater(stages[1]['end'],15)
        self.assertAlmostEqual(stages[3]['start'],28)

    def test_small_population_does_not_claim_tail_detected(self):
        stats=population_statistics([dict(episode=i,duration_s=1 if i<8 else 100) for i in range(9)])
        self.assertEqual(stats['duration_s']['tail_candidates'],[])
        self.assertIn('不足',stats['duration_s']['assessment'])

    def test_commit_refresh_annotation_and_no_source_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);root=p/'dataset';r,i=fixture(root)
            files={str(f):f.read_bytes() for f in root.rglob('*') if f.is_file()}
            store=ReviewStore(root,p/'cache',p/'logs',False);store.refresh()
            revision=store.summary()['revision'];store.refresh()
            self.assertEqual(store.summary()['revision'],revision)
            end=store.episodes[0]['summary']['duration_s']
            with self.assertRaises(ValueError):store.annotation(0,[0,1,.5,2,end])
            store.annotation(0,[0,.5,1,2,end]);store.refresh()
            self.assertEqual(store.episodes[0]['summary']['stage_status'],'人工确认')
            self.assertEqual(files,{str(f):f.read_bytes() for f in root.rglob('*') if f.is_file()})
            fixture(root,episode=1);store.refresh();self.assertEqual(len(store.summary()['rows']),2)

    def test_schema_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r,i=fixture(root);i['features']['action']['names']=['wrong']
            with self.assertRaises(ValueError):analyze_episode(root,r,i,False)

    def test_http_origin_range_and_no_dataset_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);root=p/'dataset';fixture(root)
            store=ReviewStore(root,p/'cache',p/'logs',False);store.refresh()
            video=store.paths[0][1][0];video.parent.mkdir(parents=True,exist_ok=True)
            video.write_bytes(b'0123456789')  # transport fixture, not a claimed video
            before={str(f):f.read_bytes() for f in root.rglob('*') if f.is_file()}
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(store,p/'dist'))
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            def request(method,path,body=None,headers=None):
                c=HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                try:
                    c.request(method,path,body,headers or {});r=c.getresponse()
                    return r.status,r.read()
                finally:c.close()
            try:
                self.assertEqual(request('GET','/api/review')[0],200)
                self.assertEqual(request('GET','/api/review',headers={'Host':'evil.example'})[0],403)
                self.assertEqual(request('GET','/../config/control.json')[0],404)
                self.assertEqual(request('GET','/api/video/0/0',headers={'Range':'bytes=2-5'}),(206,b'2345'))
                self.assertEqual(request('GET','/api/video/0/0',headers={'Range':'bytes=20-'})[0],416)
                body=json.dumps({'boundaries':[0,.5,1,2,store.episodes[0]['summary']['duration_s']]})
                self.assertEqual(request('POST','/api/annotations/0',body,{'Origin':'http://evil.example'})[0],403)
                self.assertEqual(request('POST','/api/annotations/0',body)[0],200)
                self.assertEqual(before,{str(f):f.read_bytes() for f in root.rglob('*') if f.is_file()})
            finally:server.shutdown();server.server_close();worker.join()


if __name__=='__main__':unittest.main()
