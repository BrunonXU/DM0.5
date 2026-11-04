"""Compare exact preprocessed RGB bytes across x86/ARM, including PNG roundtrip."""
import argparse
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import numpy as np
from PIL import Image

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--inputs',type=Path);p.add_argument('--original-episode',type=Path)
p.add_argument('--assets',type=Path,required=True)
p.add_argument('--write-reference',type=Path);p.add_argument('--reference',type=Path)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
spec=importlib.util.spec_from_file_location('rtc_processing',a.assets/'processing.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
contract=json.loads((a.assets/'contract.json').read_text());hashes={};sizes=[]
if a.original_episode:
    reference=json.loads(a.reference.read_text())
    rows=[]
    for line in (a.original_episode/'payload/frames.jsonl').open():
        row=json.loads(line)
        for frame in row['frames']:
            name=f"images/{row['observation_id']}/{frame['camera']}.jpg"
            if name not in reference:continue
            with (a.original_episode/'payload'/frame['path']).open('rb') as stream:
                stream.seek(frame['payload_offset']);raw=stream.read(frame['payload_bytes'])
            rows.append((frame['camera'],name,raw,frame['payload_sha256']))
else:
    rows=[(camera,name,(a.inputs/name).read_bytes(),row['image_sha256'][camera])
          for row in json.loads((a.inputs/'observations.json').read_text()).values()
          for camera,name in row['images'].items()]
for camera,name,raw,digest in rows:
        assert hashlib.sha256(raw).hexdigest()==digest
        with Image.open(BytesIO(raw)) as image:pixels=module.process_image(image,camera,contract)
        out=BytesIO();Image.fromarray(pixels).save(out,format='PNG',compress_level=1)
        np.testing.assert_array_equal(pixels,np.asarray(Image.open(BytesIO(out.getvalue()))))
        hashes[name]=hashlib.sha256(pixels.tobytes()).hexdigest()
        sizes.append([len(raw),len(out.getvalue())])
if a.reference:
    assert hashes==json.loads(a.reference.read_text()),'ARM/x86 model input pixels differ'
if a.write_reference:a.write_reference.write_text(json.dumps(hashes,indent=2))
a.output.write_text(json.dumps(dict(images=len(hashes),pixel_hashes_matched=bool(a.reference),
    lossless_png_roundtrip=True,original_total_bytes=sum(x[0] for x in sizes),
    prepared_total_bytes=sum(x[1] for x in sizes),robot_commands=0),indent=2))
print(a.output.read_text())
