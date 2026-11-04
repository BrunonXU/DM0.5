from pathlib import Path
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory
import cv2, numpy as np, json, math, os, re, time, traceback

ROOTS=[Path(r'd:\all_data'),Path(r'd:\all_data_1'),Path(r'd:\all_data_2'),Path(r'd:\all_data_3')]
OUT=Path(r'C:\Users\user\AppData\Local\Temp\all_episode_motion_timings.json')
CHECKPOINT=Path(r'C:\Users\user\AppData\Local\Temp\all_episode_motion_timings.jsonl')

def transform_displacement(old,new,shape,matcher):
    kp0,des0=old;kp1,des1=new
    if des0 is None or des1 is None:return None
    matches=sorted(matcher.match(des0,des1),key=lambda item:item.distance)[:350]
    if len(matches)<30:return None
    p0=np.float32([kp0[x.queryIdx].pt for x in matches]);p1=np.float32([kp1[x.trainIdx].pt for x in matches])
    affine,mask=cv2.estimateAffinePartial2D(p0,p1,method=cv2.RANSAC,ransacReprojThreshold=1.5,maxIters=1200)
    if affine is None or mask is None:return None
    valid=mask.ravel().astype(bool);n=int(valid.sum());ratio=n/len(matches)
    if n<65 or ratio<.20:return None
    h,w=shape;points=np.float32([[0,0],[w,0],[0,h],[w,h],[w/2,h/2]])
    transformed=points@affine[:,:2].T+affine[:,2]
    return float(np.median(np.linalg.norm(transformed-points,axis=1))),n,ratio

def choose_windows(clusters,duration,root_name):
    useful=[c for c in clusters if c['sum_displacement']>=4.0 and c['max_displacement']>=1.5]
    outbound_candidates=[c for c in useful if c['center']<duration*.48 and c['start']>duration*.03]
    return_candidates=[c for c in useful if c['center']>duration*.52 and c['end']<duration*.995]
    method='camera_global_motion';confidence=.55
    if outbound_candidates:
        outbound=max(outbound_candidates,key=lambda c:c['sum_displacement'])
        adjacent=[c for c in useful if c['center']<duration*.48 and c['end']>=outbound['start']-3 and c['start']<=outbound['end']+3]
        out_start=max(0,min(c['start'] for c in adjacent)-.15);out_end=min(duration,max(c['end'] for c in adjacent)+.35)
    else:
        out_start=duration*.20;out_end=duration*.245;method+=' + outbound_fallback';confidence=.25
    if return_candidates:
        ret=max(return_candidates,key=lambda c:c['sum_displacement'])
        adjacent=[c for c in useful if c['center']>duration*.52 and c['end']>=ret['start']-3 and c['start']<=ret['end']+3]
        ret_start=max(out_end+1,min(c['start'] for c in adjacent)-.35);ret_end=min(duration,max(c['end'] for c in adjacent)+.35)
    else:
        ret_start=duration*.84;ret_end=duration*.95;method+=' + return_fallback';confidence=.25
    if out_end-out_start<1.0:out_end=min(duration,out_start+1.6)
    if ret_end-ret_start<1.0:ret_end=min(duration,ret_start+1.6)
    return {'outbound_start_s':round(out_start,3),'outbound_end_s':round(out_end,3),'return_start_s':round(ret_start,3),'return_end_s':round(ret_end,3),'method':method,'confidence':confidence}

def analyze(path_text):
    path=Path(path_text);root_name=path.parents[1].name
    orb=cv2.ORB_create(nfeatures=1200,fastThreshold=12);matcher=cv2.BFMatcher(cv2.NORM_HAMMING,crossCheck=True)
    history=deque(maxlen=3);events=[];first=None;last=-10**30;duration=0.0
    with path.open('rb') as stream:
        reader=make_reader(stream,decoder_factories=[DecoderFactory()])
        for _,_,message,obj in reader.iter_decoded_messages(topics=['/camera/head/color/image_raw/compressed']):
            if first is None:first=message.log_time
            t=(message.log_time-first)/1e9;duration=t
            if message.log_time-last<450_000_000:continue
            last=message.log_time
            image=cv2.imdecode(np.frombuffer(bytes(obj.data),np.uint8),cv2.IMREAD_GRAYSCALE)
            if image is None:continue
            image=cv2.resize(image,None,fx=.35,fy=.35);current=(orb.detectAndCompute(image,None),t)
            best=0.0;best_inliers=0;best_ratio=0.0
            for prior in history:
                result=transform_displacement(prior[0],current[0],image.shape,matcher)
                if result:
                    displacement,n,ratio=result;dt=t-prior[1];rate=displacement/max(dt,.001)
                    if rate>best:best=rate;best_inliers=n;best_ratio=ratio
            if best>=1.0:events.append({'t':round(t,3),'rate':round(best,3),'inliers':best_inliers,'ratio':round(best_ratio,3)})
            history.append(current)
    clusters=[]
    for event in events:
        if not clusters or event['t']-clusters[-1][-1]['t']>1.65:clusters.append([event])
        else:clusters[-1].append(event)
    compact=[]
    for cluster in clusters:
        compact.append({'start':round(max(0,cluster[0]['t']-1.0),3),'end':cluster[-1]['t'],'center':round((cluster[0]['t']-1+cluster[-1]['t'])/2,3),'points':len(cluster),'sum_displacement':round(sum(x['rate'] for x in cluster),3),'max_displacement':round(max(x['rate'] for x in cluster),3)})
    windows=choose_windows(compact,duration,root_name)
    return {'path':str(path),'root':root_name,'episode':path.parent.name,'head_first_ns':first,'duration_s':round(duration,6),'clusters':compact,'windows':windows,'status':'ok'}

def main():
    paths=[]
    for root in ROOTS:
        for episode in sorted(root.iterdir()):
            if episode.is_dir() and re.fullmatch(r'episode_\d{6}',episode.name):
                files=list(episode.glob('*_0.mcap'))
                if files:paths.append(str(files[0]))
    done={}
    if CHECKPOINT.exists():
        for line in CHECKPOINT.read_text(encoding='utf-8').splitlines():
            try:
                item=json.loads(line);done[item['path']]=item
            except Exception:pass
    pending=[p for p in paths if p not in done];print(f'total={len(paths)} resumed={len(done)} pending={len(pending)}',flush=True)
    started=time.time()
    with ProcessPoolExecutor(max_workers=4) as pool, CHECKPOINT.open('a',encoding='utf-8') as log:
        futures={pool.submit(analyze,p):p for p in pending}
        for index,future in enumerate(as_completed(futures),1):
            path=futures[future]
            try:item=future.result()
            except Exception as exc:item={'path':path,'status':'error','error':repr(exc),'traceback':traceback.format_exc()}
            done[path]=item;log.write(json.dumps(item,ensure_ascii=False)+'\n');log.flush()
            if index%10==0 or item['status']=='error':print(f'completed={len(done)}/{len(paths)} errors={sum(x["status"]!="ok" for x in done.values())} elapsed_min={(time.time()-started)/60:.1f}',flush=True)
    ordered=[done[p] for p in paths];OUT.write_text(json.dumps(ordered,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'DONE output={OUT} total={len(ordered)} errors={sum(x["status"]!="ok" for x in ordered)} elapsed_min={(time.time()-started)/60:.1f}',flush=True)
if __name__=='__main__':main()
