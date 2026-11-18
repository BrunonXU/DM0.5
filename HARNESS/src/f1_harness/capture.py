"""Convert native ROS capture into inspectable replay without inventing alignment."""
from pathlib import Path
import hashlib
import json

from .contracts import Frame
from .observation import ObservationBuffer
from .images import decode


def inspect_capture(root: Path):
    report=json.loads((root/'capture.json').read_text())
    buffer=ObservationBuffer(source_clock_verified=False)
    infos={r['topic']:r for r in report['records'] if r['kind']=='camera_info'}
    decoded=[]
    for row in report['records']:
        if row['kind']!='image':continue
        topic=row['topic'];camera='right' if '/right_wrist/' in topic else 'head' if '/head/' in topic else topic.split('/')[2]
        stream='depth' if '/depth/' in topic else 'rgb'
        info_topic=topic.rsplit('/',1)[0]+'/camera_info'
        info=infos.get(info_topic)
        info_sha=hashlib.sha256(json.dumps(info,sort_keys=True).encode()).hexdigest() if info else None
        fields={k:row[k] for k in ('sequence','received_ns','source_ns','frame_id','width','height','encoding','row_step_bytes','is_bigendian','blob_sha256')}
        # Scale is only a conventional decoding hypothesis in this inspection;
        # it does not obtain hardware geometry admission.
        frame=Frame(**fields,camera=camera,stream=stream,clock_domain='unverified-native-ROS',
                    camera_info_sha256=info_sha,depth_scale_m=.001 if stream=='depth' else None)
        data=(root/'blobs'/frame.blob_sha256).read_bytes()
        if hashlib.sha256(data).hexdigest()!=frame.blob_sha256:raise ValueError('capture_integrity_failure')
        image=decode(frame,data)
        buffer.add(frame)
        decoded.append(dict(camera=camera,stream=stream,shape=list(image.shape),encoding=frame.encoding))
    obs=buffer.snapshot()
    return dict(decoded=decoded,observation=obs.model_dump(),geometry_admitted=False,
                missing_cameras=sorted({'head','right'}-{f.camera for f in obs.frames}),
                depth_scale_verified=False,command_publishers=report['command_publishers'])
