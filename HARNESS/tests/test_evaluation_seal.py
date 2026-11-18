import json

import pytest

from f1_harness.contracts import digest
from f1_harness.evaluation import evaluate
from f1_harness.evidence_pack import seal_run
from test_evidence_pack import fixture


def manifest():
    value={'scenarios':[{'scenario_id':str(i)} for i in range(20)]}
    return dict(value,manifest_id=digest(value))


def test_unsealed_success_trace_cannot_satisfy_physical_acceptance(tmp_path):
    frozen=manifest()
    rows=[dict(kind='run_identity',synthetic=False,scenario_id='0',heldout=True,
               scene_manifest_sha256=frozen['manifest_id']),
          dict(kind='skill_result',skill='verify_placed',result=dict(request_id='one',outcome='success',synthetic=False,assisted=False)),
          dict(kind='owner_placement_verified',request_id='one')]
    (tmp_path/'trace.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    value=evaluate(frozen,[tmp_path])
    assert value['autonomous_successes']==0
    assert not value['rows'][0]['sealed_evidence_complete']


def test_sealed_files_and_frozen_scene_must_both_match(tmp_path):
    frozen=manifest();p,o=fixture(tmp_path)
    trace=o/'harness/trace.jsonl';content=trace.read_text()
    identity=dict(event_id='run',kind='run_identity',synthetic=False,scenario_id='0',heldout=True,
                  scene_manifest_sha256=frozen['manifest_id'])
    proof=dict(event_id='proof',kind='owner_placement_verified',request_id='request-8')
    trace.write_text(json.dumps(identity)+'\n'+content+json.dumps(proof)+'\n')
    out=tmp_path/'sealed';seal_run(p,o,out)
    assert evaluate(frozen,[out])['autonomous_successes']==1
    changed={**frozen,'note':'another frozen test'}
    changed['manifest_id']=digest({k:v for k,v in changed.items() if k!='manifest_id'})
    assert evaluate(changed,[out])['autonomous_successes']==0
    with (out/'owner/harness/trace.jsonl').open('a') as f:f.write('{}\n')
    with pytest.raises(ValueError,match='hash_mismatch'):evaluate(frozen,[out])
