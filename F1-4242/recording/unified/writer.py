"""Single locked official LeRobot v3 writer; candidate != human-reviewed release."""
from io import BytesIO
import importlib
import hashlib
import json
import os
from pathlib import Path
import sys
from .contract import CAMERAS, NAMES, CONTRACT, atomic_json, json_bytes, sha256, feature_contract, validate_row, confined


def official_writer(root):
    root = Path(root).resolve(strict=True)
    binding = json.loads(Path(__file__).with_name('lerobot_binding.json').read_text())
    for rel, expected in binding['files'].items():
        if sha256(root/rel) != expected: raise ValueError('LeRobot dependency differs: '+rel)
    sys.path.insert(0, str(root/'src'))
    module = importlib.import_module('lerobot.datasets.lerobot_dataset')
    if root not in Path(module.__file__).resolve().parents: raise ValueError('wrong LeRobot already imported')
    from lerobot.configs.video import RGBEncoderConfig
    return module.LeRobotDataset, RGBEncoderConfig, binding


def selection_sha256(bundle):
    """Segment IDs only have meaning for this exact selection and time grid."""
    rows = [[[r['time_ns'], r.get('source_row'), r.get('source_command')] for r in group]
            for group in bundle.segments]
    return hashlib.sha256(json_bytes(dict(fps=bundle.metadata['fps'], segments=rows,
        resampling=bundle.metadata.get('selection', {}).get('resampling')))).hexdigest()


def review_template(bundle):
    return dict(schema='f1_data_review_v1', source_sha256=bundle.metadata['source_sha256'], reviewer='',
                selection_sha256=selection_sha256(bundle),
                task=bundle.metadata.get('task',''), decision='pending', outcome='unknown',
                accepted_segments=[], available_segments=[dict(id=i, rows=len(g),
                    start_ns=g[0]['time_ns'], end_ns=g[-1]['time_ns'], session=g[0]['session_id'])
                    for i,g in enumerate(bundle.segments)], notes='')


def export(bundle, output, lerobot_root, *, review=None, cancel=lambda: False):
    import numpy as np
    from PIL import Image
    output = Path(output).absolute()
    confined(output.parent, output.name)
    source = Path(bundle.metadata['source_root']).resolve()
    if output.resolve() == source or source in output.resolve().parents:
        raise ValueError('export must be outside immutable source')
    partial = output.with_name(output.name+'.partial')
    if output.exists() or partial.exists(): raise FileExistsError('refusing to overwrite export or recovery')
    groups = bundle.segments
    approved = False
    if review is not None:
        if review.get('schema') != 'f1_data_review_v1' or review.get('source_sha256') != bundle.metadata['source_sha256']:
            raise ValueError('review belongs to another source')
        if (bundle.metadata.get('selection', {}).get('resampling') or 'selection_sha256' in review):
            if review.get('selection_sha256') != selection_sha256(bundle):
                raise ValueError('review belongs to another selection; regenerate and review segment IDs')
        ids = review.get('accepted_segments')
        if review.get('decision') != 'accepted' or not str(review.get('reviewer','')).strip() or not str(review.get('task','')).strip():
            raise ValueError('explicit reviewer, accepted decision and concrete task required')
        if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)) or any(type(i) is not int or not 0<=i<len(groups) for i in ids):
            raise ValueError('explicit unique accepted segment IDs required')
        groups = [groups[i] for i in ids]
        approved = True
    if not groups: raise ValueError('no contiguous valid human segment; inspect blockers')
    for group in groups:
        for row in group:
            validate_row(row)
            if not all(row['action_valid']): raise ValueError('unknown action may not become supervision')
    cls, RGBEncoderConfig, binding = official_writer(lerobot_root)
    features = {key:dict(dtype='float32',shape=(16,),names=list(NAMES)) for key in ('observation.state','action')}
    for camera in CAMERAS:
        with Image.open(BytesIO(bundle.image_bytes(groups[0][0],camera))) as im: w,h=im.size
        features['observation.images.'+camera] = dict(dtype='video',shape=(h,w,3),names=['height','width','channels'])
    output.parent.mkdir(parents=True,exist_ok=True)
    writer = cls.create(repo_id='local/f1_upper16', root=partial, fps=int(bundle.metadata['fps']),
                        features=features, robot_type='f1_09_upper16_omnipicker', use_videos=True,
                        video_backend='pyav', rgb_encoder=RGBEncoderConfig(vcodec='h264',crf=18,g=2),
                        encoder_threads=1, image_writer_threads=2)
    provenance = []
    task = review['task'] if review else bundle.metadata.get('task') or 'unreviewed F1 capture'
    try:
        for episode, group in enumerate(groups):
            for frame_index,row in enumerate(group):
                if cancel(): raise RuntimeError("capture started; export interrupted, partial preserved")
                frame = {'observation.state':np.asarray(row['state'],dtype=np.float32),
                         'action':np.asarray(row['action'],dtype=np.float32),'task':task}
                for camera in CAMERAS:
                    with Image.open(BytesIO(bundle.image_bytes(row,camera))) as im: pixels=np.asarray(im.convert('RGB'))
                    key='observation.images.'+camera
                    if tuple(pixels.shape)!=features[key]['shape']: raise ValueError('camera dimensions changed')
                    frame[key]=pixels
                writer.add_frame(frame)
                provenance.append(dict(row,episode_index=episode,frame_index=frame_index))
            writer.save_episode(parallel_encoding=False)
        writer.finalize()
        with (partial/'f1_source.jsonl').open('xb') as f:
            for row in provenance: f.write(json_bytes(row))
        atomic_json(partial/'f1_contract.json',feature_contract())
        atomic_json(partial/'f1_review.json',review or review_template(bundle))
        report = dict(schema='f1_dataset_audit_v1',source=bundle.metadata,writer_commit=binding['git_commit'],
                      frames=len(provenance),episodes=len(groups),fps=int(bundle.metadata['fps']),
                      human_review_accepted=approved,structure_verified=False,training_ready=False,
                      release_status='candidate',image_transforms='none; native RGB dimensions',
                      upload_performed=False)
        atomic_json(partial/'f1_audit.json',report)
        reader = cls(repo_id='local/f1_upper16',root=partial,video_backend='pyav')
        if len(reader)!=len(provenance) or reader.num_episodes!=len(groups) or reader.fps!=bundle.metadata['fps']:
            raise ValueError('official reader count/episode/frequency mismatch')
        for index,row in enumerate(provenance):
            sample=reader[index]
            for name,expected in (('observation.state',row['state']),('action',row['action'])):
                if not np.allclose(np.asarray(sample[name]),expected,rtol=1e-6,atol=1e-5): raise ValueError('numerical readback differs: '+name)
            if int(sample['episode_index'])!=row['episode_index'] or int(sample['frame_index'])!=row['frame_index']:
                raise ValueError('episode/frame offsets differ')
            if abs(float(sample['timestamp'])-row['frame_index']/reader.fps)>1e-5: raise ValueError('dataset timeline differs')
            for camera in CAMERAS:
                key='observation.images.'+camera;pixels=np.asarray(sample[key]);h,w,_=features[key]['shape']
                if pixels.shape!=(3,h,w) or not np.isfinite(pixels).all(): raise ValueError('video readback invalid')
        if bundle.verify_source is not None: bundle.verify_source()
        report.update(structure_verified=True,reader_roundtrip_verified=True,
                      training_ready=approved and not bundle.metadata.get('synthetic',False),
                      release_status='reviewed' if approved else 'candidate')
        atomic_json(partial/'f1_audit.json',report)
        files={str(p.relative_to(partial)):sha256(p) for p in partial.rglob('*') if p.is_file()}
        atomic_json(partial/'f1_files.json',dict(files=files))
        if output.exists(): raise FileExistsError(output)
        os.rename(partial,output)
        return report
    except BaseException:
        if partial.exists(): atomic_json(partial/'failure.json',dict(training_ready=False,reason='export or reader validation interrupted; source unchanged'))
        raise
