import base64
import json
import pytest
from f1_harness.evidence import EvidenceStore
from f1_harness.llm_trace import capture, exchanges

RID='1'*32

def test_complete_exchange_preserves_context_and_scopes_images(tmp_path):
    store=EvidenceStore(tmp_path)
    payload=dict(model='gpt-6-astra',instructions='rules '*600,tools=[{'type':'function'}],
                 input=[dict(role='user',content=[dict(type='input_image',image_url='data:image/png;base64,'+base64.b64encode(b'png-bytes').decode())])])
    capture(store,RID,'request',payload)
    capture(store,RID,'attempt-1',dict(code='connect',automatic_retry=True))
    capture(store,RID,'response',dict(output=[dict(type='function_call',name='python',arguments='{}')],usage={'input_tokens':34}))
    capture(store,RID,'tool-'+'2'*16,dict(name='python',result={'outcome':'failure'}))
    listing=exchanges(tmp_path)['exchanges'];assert len(listing)==1 and listing[0]['status']=='returned' and listing[0]['attempts']==1
    full=exchanges(tmp_path,request_id=RID)
    assert full['request']['content']['instructions']==payload['instructions']
    assert len(full['parts'])==3
    ref=full['request']['content']['input'][0]['content'][0]['image_url']['image_ref']
    assert exchanges(tmp_path,request_id=RID,image_id=ref)['data_url'].endswith(base64.b64encode(b'png-bytes').decode())
    with pytest.raises(ValueError):exchanges(tmp_path,request_id=RID,image_id='a'*64)
    with pytest.raises(ValueError):exchanges(tmp_path,request_id='../secret')
    capture(store,RID,'request',{'replace':True})
    assert exchanges(tmp_path,request_id=RID)['request']['content']['model']=='gpt-6-astra'

def test_credentials_redacted_and_legacy_not_invented(tmp_path):
    store=EvidenceStore(tmp_path);assert exchanges(tmp_path)['exchanges']==[]
    capture(store,RID,'request',dict(input='Bearer abcdefghijklmnop sk-abcdefghijklmnop',authorization='secret'))
    raw=(tmp_path/f'llm-{RID}-request.json').read_text()
    assert 'abcdefghijklmnop' not in raw and '"secret"' not in raw
    p=tmp_path/f'llm-{RID}-request.json';p.unlink();outside=tmp_path/'outside';outside.write_text('{}');p.symlink_to(outside)
    with pytest.raises(ValueError):exchanges(tmp_path,request_id=RID)

def test_capture_failure_is_supplemental(tmp_path):
    class BadStore:
        root=tmp_path/'missing'
        def event(self,*a,**kw):raise OSError('disk unavailable')
    capture(BadStore(),RID,'request',dict(input='test'))
    assert not list(tmp_path.glob('llm-*'))
