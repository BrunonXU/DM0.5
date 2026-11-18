#!/usr/bin/env python3
"""Exercise pinned SAM2 image/video APIs on captured ROI; not a geometry or task success test."""
from pathlib import Path
import argparse
import hashlib
import json
import time

import cv2
import numpy as np
import torch
from f1_harness.perception import Sam2Segmenter, Sam2WindowTracker


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--checkpoint',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    torch.set_num_threads(2)
    rows=json.loads((args.capture/'capture.json').read_text())['records']
    frames=[r for r in rows if r['kind']=='image' and r['topic']=='/camera/head/color/image_raw']
    video=args.output/'video';video.mkdir()
    images=[]
    for index,row in enumerate(frames):
        if row['encoding']!='rgb8':raise ValueError('smoke expects captured head RGB8')
        data=(args.capture/'blobs'/row['blob_sha256']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=row['blob_sha256']:raise ValueError('blob changed')
        full=np.frombuffer(data,np.uint8).reshape(row['height'],row['row_step_bytes'])[:,:row['width']*3].reshape(row['height'],row['width'],3)
        roi=full[28:720,307:1229].copy();images.append(roi)
        cv2.imwrite(str(video/f'{index:05d}.jpg'),cv2.cvtColor(roi,cv2.COLOR_RGB2BGR))
    # Human-inspected checkerboard bounding rectangle, used solely as a segmentation prompt.
    box=np.array([222,365,495,568],np.float32)
    config='configs/sam2.1/sam2.1_hiera_t.yaml'
    started=time.monotonic()
    segmenter=Sam2Segmenter(config,args.checkpoint,device='cpu')
    masks=segmenter.segment(images[0],box=box)
    image_s=time.monotonic()-started
    cv2.imwrite(str(args.output/'mask.png'),masks[0][0].astype(np.uint8)*255)
    tracker=Sam2WindowTracker(config,args.checkpoint,device='cpu')
    started=time.monotonic()
    tracks=tracker.track(video,source_stamps=[r['source_ns'] for r in frames],box=box)
    report=dict(device='cpu',checkpoint_sha256=hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
        image_mask_pixels=int(masks[0][0].sum()),image_score=masks[0][1],image_load_and_infer_s=image_s,
        tracking_s=time.monotonic()-started,tracking_frames=len(tracks),
        areas=[int(r['masks'][1].sum()) for r in tracks],metric_pose_verified=False,robot_actions=0)
    (args.output/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
