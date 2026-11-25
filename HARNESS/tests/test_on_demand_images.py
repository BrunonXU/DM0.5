import io
import json
from copy import deepcopy

import numpy as np
import pytest
from PIL import Image
from f1_harness.sandbox import SandboxedArm
from f1_harness.evidence import EvidenceStore
from f1_harness.planner import ResponsesPlanner
from test_planner_skill_feedback import Backend,result


class Images(Backend):
    def __init__(self):
        super().__init__(result());self.fetched=[]
        # Larger than the 64 KiB control IPC: exercise the actual chunk path.
        stream=io.BytesIO()
        Image.fromarray(np.random.default_rng(7).integers(0,256,(240,320,3),dtype=np.uint8)).save(stream,format='PNG')
        self.data=stream.getvalue()
    def artifact(self,name,step=-1):
        assert name in ('head.png','right.png','left.png') and step==-1
        self.fetched.append(name);return self.data


def test_actual_isolated_worker_transfers_only_requested_image_and_crop(tmp_path):
    backend=Images();arm=SandboxedArm(backend,tmp_path/'task').start()
    try:
        plain=arm.execute_tool('python',{'code':'robo.observe()'})
        assert not plain.images and not backend.fetched
        shown=arm.execute_tool('python',{'code':'robo.show("right")'})
        assert not shown.result.get('exception'),shown.result
        assert shown.images==[backend.data] and backend.fetched==['right.png']
        crop=arm.execute_tool('python',{'code':'display(robo.image("right")[10:50,20:80])'})
        assert not crop.result.get('exception'),crop.result
        assert Image.open(io.BytesIO(crop.images[0])).size==(60,40)
        assert not list((tmp_path/'task').glob('feedback-*.png'))
    finally:arm.close()


@pytest.mark.parametrize('image_reason',[None,'Numeric state cannot identify the visible block; inspect the right view.'])
@pytest.mark.parametrize('camera',['right','left'])
def test_planner_receives_real_image_bytes_on_next_turn_without_api(tmp_path,image_reason,camera):
    backend=Images();arm=SandboxedArm(backend,tmp_path/'task').start()
    planner=object.__new__(ResponsesPlanner);planner.store=EvidenceStore(tmp_path/'evidence')
    requests=[]
    def request(**kwargs):
        requests.append(deepcopy(kwargs));first=len(requests)==1
        return {'output':[dict(type='function_call',call_id=str(len(requests)),name='python' if first else 'finish',
            arguments=json.dumps(dict(code=f'robo.show({camera!r})',**({'image_reason':image_reason} if image_reason else {}))
                if first else {'status':'failure','summary':'fixture complete'}))]}
    planner.request=request
    try:
        planner.run(arm,task_text='synthetic',max_turns=2)
        images=[c for item in requests[1]['input'] if isinstance(item.get('content'),list)
                  for c in item['content'] if c.get('type')=='input_image']
        if image_reason:
            assert len(images)==1 and images[0]['image_url'].startswith('data:image/png;base64,')
        else:
            assert not images  # Local image access is permitted; model delivery is not.
        assert 'base64' not in (tmp_path/'evidence/trace.jsonl').read_text()
    finally:arm.close()


def test_single_block_consults_model_with_images_once_across_stages(tmp_path):
    backend = Images()
    arm = SandboxedArm(backend, tmp_path / 'task').start()
    planner = object.__new__(ResponsesPlanner)
    planner.store = EvidenceStore(tmp_path / 'evidence')
    calls = [
        ('python', dict(code='robo.show("left")')),
        ('python', dict(code='robo.show("head")', image_reason='The fixture landmarks are ambiguous.')),
        ('python', dict(code='robo.show("right")', image_reason='I need to distinguish the aperture.')),
        ('finish', dict(status='failure', summary='The aperture pose is unknown.')),
    ]
    image_counts = []

    def request(**kwargs):
        name, args = calls[len(image_counts)]
        parts = (part for item in kwargs['input'] if isinstance(item.get('content'), list)
                 for part in item['content'])
        image_counts.append(sum(part.get('type') == 'input_image' for part in parts))
        return {'output': [dict(type='function_call', name=name, call_id=str(len(image_counts)),
                                arguments=json.dumps(args))]}

    planner.request = request
    planner.single_visual_consultation = True
    try:
        planner.run(arm, task_text='single block')
    finally:
        arm.close()
    assert image_counts == [0, 0, 1, 0]
    assert backend.fetched == ['left.png', 'head.png', 'right.png']
    events = [json.loads(line) for line in (planner.store.root / 'trace.jsonl').read_text().splitlines()]
    withheld = [event for event in events if event.get('reason') == 'single_block_visual_consultation_used']
    assert len(withheld) == 1 and planner.store.blob(withheld[0]['refs'][0]) == backend.data
