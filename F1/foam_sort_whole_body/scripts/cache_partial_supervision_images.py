#!/usr/bin/env python3
"""Extend original-frame caches for preserved arm windows; reuse verified bytes.

Old payloads are referenced, not copied. New payloads are extracted from MCAP.
Every output remains candidate until full independent image readback passes.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import json
from pathlib import Path
import sys
import numpy as np
from PIL import Image
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory
TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'models/DM0.5'))
from f1_data.reader import Episode, CAMERAS, sha256
from f1_data.images import TOPICS


def write(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def one(job):
    row,pool,output,reuse,raw_root=job;stem=row['episode'].replace('/','_')
    ep=Episode(pool/'cases'/stem/'composed/episode.npz',pool/'pose_cache'/stem/'pose_cache.npz',allow_candidates=True)
    dest=output/stem;dest.mkdir(parents=True,exist_ok=True);manifest=dest/'manifest.json'
    if manifest.exists():
        prior=json.loads(manifest.read_text())
        assert prior['source_npz_sha256']==ep.source_sha256 and prior['checked_starts']==ep.starts.tolist()
        return dict(episode=ep.episode,status='complete',resumed=True)
    raw=raw_root/ep.meta['source_relative_path'];stat=raw.stat()
    assert (stat.st_size,stat.st_mtime_ns)==(ep.meta['source_size_bytes'],ep.meta['source_mtime_ns'])
    required={name:set(map(int,ep.data['camera_source_indices'][ep.starts,i]))for i,name in enumerate(CAMERAS)}
    entries={name:{}for name in CAMERAS};oldpath=reuse/stem/'manifest.json';reused=0
    if oldpath.exists():
        old=json.loads(oldpath.read_text())
        assert old['source_npz_sha256']==row['provenance_sha256']
        assert (old['raw_size_bytes'],old['raw_mtime_ns'])==(stat.st_size,stat.st_mtime_ns)
        for name in CAMERAS:
            for index in required[name]:
                entry=old['images'][name].get(str(index))
                if entry is None:continue
                original=(oldpath.parent/entry['path']).resolve();target=dest/entry['path'];target.parent.mkdir(exist_ok=True)
                assert original.is_file()
                if target.exists() or target.is_symlink():assert target.is_symlink() and target.resolve()==original
                else:target.symlink_to(original)
                entries[name][str(index)]=entry;reused+=1
    missing={name:required[name]-set(map(int,entries[name]))for name in CAMERAS}
    counts={name:0 for name in CAMERAS};reverse={v:k for k,v in TOPICS.items()};decoder=DecoderFactory();extracted=0
    if any(missing.values()):
        with raw.open('rb')as stream:
            for schema,channel,message in make_reader(stream,validate_crcs=True).iter_messages(topics=list(TOPICS.values()),log_time_order=True):
                name=reverse[channel.topic];index=counts[name];counts[name]+=1
                if index not in missing[name]:continue
                decode=decoder.decoder_for(channel.message_encoding,schema);assert decode is not None
                message_data=decode(message.data);payload=bytes(message_data.data)
                with Image.open(io.BytesIO(payload))as image:image.load();size=list(image.size)
                filename=f'{name}/{index:08d}.image';target=dest/filename;target.parent.mkdir(exist_ok=True)
                assert not target.is_symlink()
                tmp=target.with_suffix('.tmp');tmp.write_bytes(payload);tmp.replace(target)
                entries[name][str(index)]=dict(path=filename,sha256=sha256(target),size=size,
                    log_time_ns=int(message.log_time),header_time_ns=int(message_data.header.stamp.sec)*10**9+int(message_data.header.stamp.nanosec))
                extracted+=1
    for name in CAMERAS:assert set(map(int,entries[name]))==required[name]
    assert (raw.stat().st_size,raw.stat().st_mtime_ns)==(stat.st_size,stat.st_mtime_ns)
    write(manifest,dict(schema='f1_original_compressed_images_v1',episode=ep.episode,source_npz_sha256=ep.source_sha256,
        raw_path=str(raw),raw_size_bytes=stat.st_size,raw_mtime_ns=stat.st_mtime_ns,checked_starts=ep.starts.tolist(),
        full_valid_starts=True,training_approved=False,images=entries,reused_payloads=reused,new_payloads=extracted,
        reused_manifest_sha256=sha256(oldpath)if oldpath.exists()else None))
    return dict(episode=ep.episode,status='complete',reused_payloads=reused,new_payloads=extracted)


def main():
    ap=argparse.ArgumentParser()
    for name in ['pool','output','reuse','raw-root']:ap.add_argument('--'+name,type=Path,required=True)
    ap.add_argument('--workers',type=int,default=4);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    selection=json.loads((args.pool/'selection.json').read_text());rows=selection['records'];done=[]
    with ProcessPoolExecutor(max_workers=args.workers)as pool:
        futures=[pool.submit(one,(r,args.pool.resolve(),args.output.resolve(),args.reuse.resolve(),args.raw_root.resolve()))for r in rows]
        for future in as_completed(futures):
            done.append(future.result());progress=dict(complete=len(done),total=len(rows),new_payloads=sum(r.get('new_payloads',0)for r in done))
            write(args.output/'progress.json',progress)
            if len(done)%25==0:print(json.dumps(progress),flush=True)
    write(args.output/'extraction_report.json',dict(status='extracted_pending_readback',training_approved=False,episodes=done,
        selection_sha256=sha256(args.pool/'selection.json'),script_sha256=sha256(__file__)))


if __name__=='__main__':main()
