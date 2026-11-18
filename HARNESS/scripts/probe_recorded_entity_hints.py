"""One actual Astra localization request on saved RGB; no robot or motion tools.

This tests model hints and production coordinate mapping, not the full planner
loop. No reviewed pixel coordinates are included in the model input.
"""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path

from PIL import Image
from f1_harness.contracts import Observation
from f1_harness.evidence import EvidenceStore
from f1_harness.images import decode
from f1_harness.planner import ResponsesPlanner
from f1_harness.target_relocalization import source_search_box, rectified_search_box


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cameras', nargs='+', choices=['head', 'right'], default=['head', 'right'])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    store = EvidenceStore(args.output)
    capture = json.loads(args.capture.read_text())
    obs = Observation.model_validate(capture['snapshots'][0]['observation'])
    geometry = capture['configuration']['scene']['geometry']
    views = {'right': geometry, 'head': geometry['hole_camera']}
    roles = {'square-1': 'loose wooden square block, not the bucket or gripper',
             'square-hole-1': 'square opening in the bucket lid',
             'bucket-triangle': 'triangular opening in the bucket lid',
             'bucket-left-slot': 'narrow vertical opening at the left of the lid',
             'bucket-u-slot': 'wide bottom opening with a central inward tab',
             'bucket-lid': 'whole circular bucket lid including its openings'}
    content = [dict(type='input_text', text=(
        'Locate visible registered entities for SAM segmentation. Return one locate_entities '
        'call per shown camera: head all visible IDs; right only square-1. '
        'Boxes are tight [left,top,right,bottom], normalized 0..1 relative to each shown image. '
        'Include positive points inside each entity and optional negative points outside it. '
        'For openings select their interior, not the surrounding lid. Omit invisible entities; '
        'do not infer hidden positions. This is saved real imagery, no robot motion. IDs: '
        + json.dumps(roles)))]
    metadata = {}
    for camera in args.cameras:
        frame = next(f for f in obs.frames if f.camera == camera and f.stream == 'rgb')
        raw = (args.capture.parent / 'blobs' / frame.blob_sha256).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == frame.blob_sha256
        rgb = decode(frame, raw)
        roi = views[camera]['roi']
        x0, y0, x1, y1 = roi
        shown = Image.fromarray(rgb[y0:y1, x0:x1])
        buffer = io.BytesIO()
        shown.save(buffer, format='PNG')
        payload = buffer.getvalue()
        (args.output / (camera + '.png')).write_bytes(payload)
        metadata[camera] = dict(camera=camera, frame=frame.model_dump(mode='json'),
            observation_id=obs.observation_id, roi=roi,
            model_image_size=list(shown.size), source_image_size=[frame.width, frame.height],
            image_sha256=hashlib.sha256(payload).hexdigest())
        content += [dict(type='input_text', text='Camera: ' + camera),
                    dict(type='input_image', image_url='data:image/png;base64,' +
                         base64.b64encode(payload).decode())]
    vector = dict(type='array', items=dict(type='number'))
    entity = dict(type='object', properties=dict(
        object_id=dict(type='string', enum=list(roles)),
        box_normalized=dict(vector, minItems=4, maxItems=4),
        points_normalized=dict(type='array', items=dict(vector, minItems=2, maxItems=2)),
        point_labels=dict(type='array', items=dict(type='integer', enum=[0, 1]))),
        required=['object_id', 'box_normalized', 'points_normalized', 'point_labels'],
        additionalProperties=False)
    tool = dict(type='function', name='locate_entities', strict=True,
        description='Provide image-only search hints; does not establish pose or authorize motion.',
        parameters=dict(type='object', properties=dict(
            camera=dict(type='string', enum=['head', 'right']),
            entities=dict(type='array', items=entity, minItems=1, maxItems=8)),
            required=['camera', 'entities'], additionalProperties=False))
    planner = ResponsesPlanner(root / 'API.MD', store)
    try:
        response = planner.request(input=[dict(role='user', content=content)],
            tools=[tool], tool_choice='required')
    finally:
        planner.client.close()
    hints = {}
    for call in response['output']:
        if call['type'] != 'function_call':
            continue
        if call['name'] != 'locate_entities':
            raise ValueError('unexpected_tool')
        parameters = json.loads(call['arguments'])
        camera = parameters['camera']
        for entity in parameters['entities']:
            identity = entity['object_id']
            if identity not in roles or (camera == 'right' and identity != 'square-1'):
                raise ValueError('unregistered_view_entity')
            if identity in hints.setdefault(camera, {}):
                raise ValueError('duplicate_entity_hint')
            source = source_search_box(metadata[camera], entity['box_normalized'],
                points_normalized=entity['points_normalized'], point_labels=entity['point_labels'])
            hints[camera][identity] = rectified_search_box(source, views[camera])
    receipt = dict(scope='actual model image hints + production coordinate mapping only',
        api_calls=1, robot_connections=0, motion_outputs=0, source_clock_unchanged=True,
        model=response['model'], reasoning=response['reasoning'], usage=response.get('usage'),
        response_id=response['id'], capture_sha256=hashlib.sha256(args.capture.read_bytes()).hexdigest(),
        metadata=metadata, hints=hints)
    store.write_json('hints.json', receipt)
    print(json.dumps(dict(model=receipt['model'], usage=receipt['usage'],
                         entities={view:list(items) for view,items in hints.items()})))


if __name__ == '__main__':
    main()
