#!/usr/bin/env python3
"""Read existing MCAPs; write 10 Hz review-only media outside raw data.

Uses bag/log timestamps as one common review clock, never as training alignment.
Missing/stale camera frames become labeled blank frames, not old imagery.
"""
from pathlib import Path
import argparse,hashlib,json,math,os,shutil,subprocess,sys,time,uuid

NAMES=[f'arm_{side}_j{i}' for side in ('l','r') for i in range(1,8)]+['left_gripper_position','right_gripper_position']
CAMERAS={'/camera/head/color/image_raw/compressed':'head','/camera/left_wrist/color/image_raw/compressed':'left','/camera/right_wrist/color/image_raw/compressed':'right'}
STATE_TOPIC='/hal/joint_states';ACTION_TOPIC='/lead/joint_states'

def build(root,relative,runtime,analytics_only=False):
    import cv2,numpy as np,yaml,rosbag2_py
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import CompressedImage,JointState
    from control_msgs.msg import GripperCommand
    source=root/relative
    if '..' in Path(relative).parts or source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):raise ValueError('source path outside configured root')
    raw=(source/'metadata.yaml').read_bytes();meta=yaml.safe_load(raw)['rosbag2_bagfile_information']
    start=int(meta['starting_time']['nanoseconds_since_epoch']);duration=int(meta['duration']['nanoseconds'])
    files=[]
    for p in source.rglob('*'):
        if p.is_symlink():raise ValueError('source symlink refused')
        if p.is_file():st=p.stat();files.append([str(p.relative_to(source)),st.st_size,st.st_mtime_ns])
    metadata_sha=hashlib.sha256(raw).hexdigest()
    signature=hashlib.sha256(json.dumps([str(root),relative,sorted(files),metadata_sha],sort_keys=True).encode()).hexdigest()
    eid=hashlib.sha256(relative.encode()).hexdigest()[:24]
    output=runtime/'data_previews'/eid
    if (output/'manifest.json').exists() and json.loads((output/'manifest.json').read_text()).get('source_signature')==signature and not analytics_only:
        print(json.dumps({'episode':relative,'status':'already_ready'}),flush=True);return
    stage=runtime/'data_previews'/('.build-'+eid+'-'+uuid.uuid4().hex)
    stage.mkdir(parents=True)
    fps=10;period=100_000_000;width,height=640,360
    blank=np.full((height,width,3),40,dtype=np.uint8)
    cv2.putText(blank,'NO RECORDED FRAME',(95,180),cv2.FONT_HERSHEY_SIMPLEX,.8,(215,215,215),2)
    encoders={};logs={}
    for camera in (() if analytics_only else CAMERAS.values()):
        logs[camera]=(stage/(camera+'.ffmpeg.log')).open('wb')
        encoders[camera]=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s',f'{width}x{height}','-r',str(fps),'-i','pipe:0','-an','-c:v','libx264','-preset','ultrafast','-crf','27','-pix_fmt','yuv420p','-threads','1','-movflags','+faststart',str(stage/(camera+'.mp4'))],stdin=subprocess.PIPE,stderr=logs[camera],stdout=subprocess.DEVNULL)
    latest={};decoded={};state={};action={};timestamps=[];states=[];actions=[];gaps={c:[] for c in CAMERAS.values()};frame_counts={c:0 for c in CAMERAS.values()}
    def row(values,at,age):
        return [values[name][1] if name in values and 0<=at-values[name][0]<=age else None for name in NAMES]
    def emit(at):
        timestamps.append(round((at-start)/1e9,4));states.append(row(state,at,250_000_000));actions.append(row(action,at,500_000_000))
        for camera in (() if analytics_only else CAMERAS.values()):
            entry=latest.get(camera);image=blank
            if entry and 0<=at-entry[0]<=250_000_000:
                if camera not in decoded or decoded[camera][0]!=entry[0]:
                    src=cv2.imdecode(np.frombuffer(entry[1],dtype=np.uint8),cv2.IMREAD_COLOR)
                    if src is not None:
                        h,w=src.shape[:2];scale=min(width/w,height/h);resized=cv2.resize(src,(max(1,round(w*scale)),max(1,round(h*scale))))
                        canvas=np.zeros((height,width,3),dtype=np.uint8);rh,rw=resized.shape[:2];canvas[(height-rh)//2:(height-rh)//2+rh,(width-rw)//2:(width-rw)//2+rw]=resized
                        decoded[camera]=(entry[0],canvas)
                if camera in decoded and decoded[camera][0]==entry[0]:image=decoded[camera][1]
            if image is blank:
                t=(at-start)/1e9
                if gaps[camera] and abs(gaps[camera][-1][1]-t)<.001:gaps[camera][-1][1]=round(t+.1,4)
                else:gaps[camera].append([round(t,4),round(t+.1,4)])
            encoders[camera].stdin.write(image.tobytes())
    topics=[*([] if analytics_only else CAMERAS),STATE_TOPIC,ACTION_TOPIC,*[f'/motion_ctl/gripper/{s}{suffix}' for s in ('left','right') for suffix in ('','/state')]]
    reader=rosbag2_py.SequentialReader();reader.open(rosbag2_py.StorageOptions(uri=str(source),storage_id='mcap'),rosbag2_py.ConverterOptions('',''));reader.set_filter(rosbag2_py.StorageFilter(topics=topics))
    due=start
    try:
        while reader.has_next():
            topic,data,stamp=reader.read_next()
            while due<stamp and due<start+duration:emit(due);due+=period
            if topic in CAMERAS:
                msg=deserialize_message(data,CompressedImage);camera=CAMERAS[topic];latest[camera]=(stamp,bytes(msg.data));frame_counts[camera]+=1
            elif topic in (STATE_TOPIC,ACTION_TOPIC) or topic.endswith('/state'):
                msg=deserialize_message(data,JointState);target=action if topic==ACTION_TOPIC else state
                for name,value in zip(msg.name,msg.position):
                    name=name.replace('arm_L_joint','arm_l_j').replace('arm_R_joint','arm_r_j')
                    if name in NAMES and math.isfinite(value):target[name]=(stamp,float(value))
            elif topic.startswith('/motion_ctl/gripper/'):
                msg=deserialize_message(data,GripperCommand);side=topic.rsplit('/',1)[-1]
                if math.isfinite(msg.position):action[side+'_gripper_position']=(stamp,float(msg.position))
        while due<start+duration:emit(due);due+=period
        for p in encoders.values():p.stdin.close()
        for camera,p in encoders.items():
            if p.wait(timeout=60):raise RuntimeError(camera+' encoder failed')
        # Reject a changed source before publishing the derived cache.
        for name,size,mtime in files:
            st=(source/name).stat()
            if st.st_size!=size or st.st_mtime_ns!=mtime:raise RuntimeError('source changed during preview build')
        notes=['仅供人工审阅：10Hz预览按bag/log时间对齐，不作为训练数据对齐。','画面缺失或距最后记录超过250ms显示空白；曲线未知值为null。','旧LXL action曲线来自/lead/joint_states及夹爪命令，不是执行ACK；未收到有效指令时不以反馈回填。']
        analytics={'ok':True,'timestamps_s':timestamps,'state':states,'action':actions,'channel_names':[f'{side}臂J{i}' for side in ('左','右') for i in range(1,8)]+['左夹爪','右夹爪'],'units':['deg']*14+['%']*2,'duration_s':duration/1e9,'sample_count':len(timestamps),'downsampled':True,'source_frame_count':next((int(t['message_count']) for t in meta.get('topics_with_message_count',[]) if t.get('topic_metadata',{}).get('name')=='/camera/head/color/image_raw/compressed'),0),'source_message_count':meta['message_count'],'notes':notes,'camera_gaps':gaps,'time_basis':'bag_log_timestamp'}
        (stage/'analytics.json').write_text(json.dumps(analytics,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
        if analytics_only:
            previous=json.loads((output/'manifest.json').read_text())
            if previous['source_signature']!=signature:raise RuntimeError('source changed; regenerate media')
            analytics['camera_gaps']=previous['camera_gaps']
            (stage/'analytics.json').write_text(json.dumps(analytics,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
            os.replace(stage/'analytics.json',output/'analytics.json');stage.rmdir()
            print(json.dumps({'episode':relative,'status':'analytics_ready','samples':len(timestamps),'action_nonnull':[sum(r[i] is not None for r in actions) for i in range(16)]}),flush=True);return
        manifest={'version':1,'episode_id':eid,'source_relative_path':relative,'source_signature':signature,'metadata_sha256':metadata_sha,'created_unix':time.time(),'fps':fps,'notes':notes,'camera_source_counts':frame_counts,'camera_gaps':gaps,'videos':[{'camera':c,'url':f'/api/data/video/{eid}/{c}.mp4','offset_s':0,'duration_s':len(timestamps)/fps} for c in CAMERAS.values()]}
        (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        if output.exists():
            archive=output.with_name(output.name+'.previous-'+str(int(time.time())));output.rename(archive)
        stage.rename(output)
        print(json.dumps({'episode':relative,'status':'ready','seconds':duration/1e9,'frames':len(timestamps),'camera_source_counts':frame_counts,'output':str(output)}),flush=True)
    finally:
        for p in encoders.values():
            if p.poll() is None:p.kill();p.wait()
        for log in logs.values():log.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-root',default='/home/user/lxl/pico_data');parser.add_argument('--runtime',required=True);parser.add_argument('--episode',action='append');parser.add_argument('--analytics-only',action='store_true');args=parser.parse_args()
    root=Path(args.source_root).absolute();runtime=Path(args.runtime).resolve()
    if runtime.is_relative_to(root.resolve()):raise ValueError('preview output must be outside source')
    episodes=args.episode or [str(p.parent.relative_to(root)) for p in sorted((root/'all_data').glob('episode_*/metadata.yaml'))]
    for relative in episodes:build(root,relative,runtime,args.analytics_only)
if __name__=='__main__':main()
