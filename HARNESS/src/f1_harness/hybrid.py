"""One-shot placement advice. Spatial triggers are advisory, never motion permits."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import numpy as np

from .geometry import transform


MODE = 'vla_astra'
LABEL = 'VLA+Astra混合策略'


@dataclass(frozen=True)
class PlacementRegion:
    hole_pose: tuple
    radius_m: float
    height_m: float
    bottom_m: float

    def __post_init__(self):
        transform(self.hole_pose)
        if not (0 < self.radius_m <= .5 and 0 < self.height_m <= .6
                and -.3 <= self.bottom_m < self.height_m):
            raise ValueError('invalid_hybrid_advisory_region')

    def contains(self, pose):
        relative = np.linalg.inv(transform(self.hole_pose)) @ transform(pose)
        x, y, z = relative[:3, 3]
        return bool(np.hypot(x, y) <= self.radius_m and self.bottom_m <= z <= self.height_m)


class PlacementOpportunity:
    """One request per measured open/close cycle, including KEEP and failed requests.

    The region uses a calibrated fixture reference only to schedule advice.
    Actual correction requires a new measured scene in the native owner.
    """
    def __init__(self, region, *, open_range):
        self.region = region
        self.open_range = tuple(open_range)
        if len(self.open_range) != 2 or not 0 <= self.open_range[0] < self.open_range[1] <= 100:
            raise ValueError('invalid_measured_gripper_open_range')
        self.cycle = 0
        self.was_open = True
        self.requested = set()
        self.closed = False

    def update(self, *, gripper, pose, scope):
        if not np.isfinite(gripper) or not 0 <= gripper <= 100:
            raise ValueError('invalid_measured_gripper')
        opened = self.open_range[0] <= gripper <= self.open_range[1]
        if self.was_open and not opened:
            self.cycle += 1
        self.was_open, self.closed = opened, not opened
        identity = (*scope, self.cycle)
        inside = self.region.contains(pose)
        return dict(identity=identity, inside=inside, closed=self.closed,
                    eligible=self.closed and inside and identity not in self.requested)

    def claim(self, state):
        if not state['eligible'] or state['identity'] in self.requested:
            raise ValueError('hybrid_opportunity_already_consumed')
        self.requested.add(state['identity'])
        return state['identity']

    @staticmethod
    def applicable(original, current):
        return bool(tuple(original) == tuple(current['identity']) and current['inside'] and current['closed'])


REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'decision': {'type': 'string', 'enum': ['KEEP', 'REFINE']},
        'reason': {'type': 'string'},
        'alignment_quarter_turns': {'type': 'integer', 'enum': [0, 1, 2, 3]},
        'retreat_first': {'type': 'boolean'},
        'head_entities': {'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{'object_id':{'type':'string','enum':['square-1','square-hole-1','bucket-lid',
                'bucket-triangle','bucket-left-slot','bucket-u-slot']},
                'box_normalized':{'type':'array','items':{'type':'number'},'minItems':4,'maxItems':4}},
            'required':['object_id','box_normalized']}},
        'right_entities': {'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{'object_id':{'type':'string','enum':['square-1']},
                'box_normalized':{'type':'array','items':{'type':'number'},'minItems':4,'maxItems':4}},
            'required':['object_id','box_normalized']}},
    },
    'required': ['decision', 'reason', 'alignment_quarter_turns', 'retreat_first','head_entities','right_entities'],
}


def checked_review(value):
    if not isinstance(value, dict) or set(value) != set(REVIEW_SCHEMA['required']):
        raise ValueError('hybrid_review_fields')
    if (value['decision'] not in ('KEEP', 'REFINE') or type(value['retreat_first']) is not bool
            or type(value['alignment_quarter_turns']) is not int
            or value['alignment_quarter_turns'] not in range(4)
            or not isinstance(value['reason'], str) or not 0 < len(value['reason']) <= 1200):
        raise ValueError('hybrid_review_values')
    for camera in ('head','right'):
        entities=value[camera+'_entities'];allowed=REVIEW_SCHEMA['properties'][camera+'_entities']['items']['properties']['object_id']['enum']
        if not isinstance(entities,list) or len(entities)>len(allowed):raise ValueError('hybrid_localization_count')
        seen=set()
        for entity in entities:
            if (not isinstance(entity,dict) or set(entity)!={'object_id','box_normalized'}
                    or entity['object_id'] not in allowed or entity['object_id'] in seen):
                raise ValueError('hybrid_localization_identity')
            seen.add(entity['object_id']);box=entity['box_normalized']
            if (not isinstance(box,list) or len(box)!=4 or any(type(v) not in (int,float) for v in box)
                    or not all(0<=v<=1 for v in box) or box[0]>=box[2] or box[1]>=box[3]):
                raise ValueError('hybrid_localization_box')
    return dict(value)


def review_once(planner, snapshot, images):
    """Exactly one Responses generation; no Python tools or follow-up tool loop."""
    content = [dict(type='input_text', text=json.dumps(snapshot, ensure_ascii=False, allow_nan=False))]
    for camera, pixels in images.items():
        content += [dict(type='input_text', text='Current '+camera+' camera'),
                    dict(type='input_image', image_url='data:image/png;base64,'+
                         base64.b64encode(pixels).decode())]
    response = planner.request(input=[dict(role='user', content=content)], tools=[],
        text={'format': {'type': 'json_schema', 'name': 'placement_review',
                         'strict': True, 'schema': REVIEW_SCHEMA}},
        instructions=(
            'Review ONLY the current square-block placement. B1250 RTC is already moving; '
            'this is one asynchronous consultation, not permission for past actions. '
            'Choose KEEP unless the images and measurements support a useful local correction. '
            'REFINE requests the existing measured align/insert/release procedure. '
            'Choose a square-symmetric hole-relative alignment (0,1,2,3 quarter turns), '
            'and whether to retreat above the opening before alignment. No absolute coordinates, '
            'For REFINE also provide tight visible-entity boxes in the displayed head/right images, '
            '[left,top,right,bottom] normalized 0..1. In head identify the wooden square block, '
            'square aperture, bucket lid and the triangle/left-slot/U-slot fixture apertures when visible; '
            'in right identify only the wooden block. Omit invisible entities. These are automatic SAM '
            'search hints, never pose/attachment evidence. KEEP may use empty lists. '
            'new grasp, transport, return, shell or tool program. Robot code remeasures, checks '
            'IK/collision and rejects stale suggestions. Unknown geometry is not success. '
            'Explain the visible reason briefly in Chinese. Return only the specified JSON.'))
    messages = [part['text'] for item in response.get('output', []) if item.get('type') == 'message'
                for part in item.get('content', []) if part.get('type') == 'output_text']
    if len(messages) != 1:
        raise ValueError('hybrid_review_missing_structured_response')
    return checked_review(json.loads(messages[0]))
