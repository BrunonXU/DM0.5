"""Offline evidence must distinguish precontact, closing, and loaded travel."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from grasp_output_sweeps import grasp_phases


def evidence():
    rows=[]
    for stamp,grip in [(10,0),(20,100),(30,100),(40,100)]:
        q=[0.]*16;q[15]=grip
        rows.append(dict(virtual_ns=stamp,submitted16=q,reference16=q.copy(),simulation=True))
    plan=dict(geometry_trace=[dict(start_index=0,end_index=2,semantic_stage='grasp_close'),dict(start_index=2,end_index=3,semantic_stage='grasp_lift')],
              targets=[{}, {}, {}],trajectory=dict(anchor=[0.]*16))
    events=[dict(kind='harness_measured_waypoint',request_id='grasp',index=i,monotonic_ns=t)
            for i,t in [(0,15),(1,30),(2,40)]]
    return rows,plan,events


def test_commanded_closure_splits_contact_from_loaded_motion():
    rows,plan,events=evidence()
    result=grasp_phases(rows,plan,events,'grasp',0,100)
    assert result['phases']==['preclosure','closure_hold','closure_hold','support_lift']
    assert result['closure_command_arm_drift_deg']==0


@pytest.mark.parametrize('fault,reason',[
    ('early_close','closure_before_precontact_arrival'),
    ('missing_closed_arrival','closure_arrival_missing'),
    ('reopen','gripper_reopened'),
    ('arm_move','arm_reference_changed'),
    ('old_clock','grasp_output_clock_invalid'),
    ('physical_row','offline_command_evidence_required'),
])
def test_rejects_invalid_contact_or_recording_sequence(fault,reason):
    rows,plan,events=evidence()
    if fault=='early_close':events[0]['monotonic_ns']=20
    elif fault=='missing_closed_arrival':events.pop(1)
    elif fault=='reopen':rows[-1]['submitted16'][15]=0
    elif fault=='arm_move':rows[2]['reference16'][7]=1
    elif fault=='old_clock':rows[2]['virtual_ns']=rows[1]['virtual_ns']
    elif fault=='physical_row':rows[0]['simulation']=False
    with pytest.raises(ValueError,match=reason):grasp_phases(rows,plan,events,'grasp',0,100)


@pytest.mark.parametrize('cutoff',[1,2,3])
def test_failed_grasp_prefix_is_audited_without_inventing_lift(cutoff):
    rows,plan,events=evidence();rows=rows[:cutoff]
    events=[e for e in events if e['monotonic_ns']<=rows[-1]['virtual_ns']]
    result=grasp_phases(rows,plan,events,'grasp',0,100,complete=False)
    assert not result['plan_completed']
    assert result['lift_arrival_ns'] is None
    assert len(result['phases'])==cutoff
    assert (result['closure_row'] is None)==(cutoff==1)


def regrasp_evidence(exit_stage=True):
    names=['regrasp_open',*(['regrasp_exit'] if exit_stage else []),'regrasp_clearance','grasp_close','grasp_lift']
    plan=dict(geometry_trace=[],targets=[],trajectory=dict(anchor=[0.]*15+[100.]))
    rows=[];events=[];stamp=10;index=0
    for name in names:
        start=index
        if name=='grasp_close':
            q=[0.]*16
            rows.append(dict(virtual_ns=stamp,submitted16=q,reference16=q.copy(),simulation=True))
            events.append(dict(kind='harness_measured_waypoint',request_id='grasp',index=index,monotonic_ns=stamp+5))
            stamp+=10;index+=1
        grip=100 if name in ('grasp_close','grasp_lift') else 0
        q=[0.]*15+[grip]
        rows.append(dict(virtual_ns=stamp,submitted16=q,reference16=q.copy(),simulation=True))
        events.append(dict(kind='harness_measured_waypoint',request_id='grasp',index=index,monotonic_ns=stamp+5))
        stamp+=10;index+=1
        plan['geometry_trace'].append(dict(semantic_stage=name,start_index=start,end_index=index,
            target_contact_hold=name=='regrasp_open' and exit_stage,target_contact_departure=name=='regrasp_exit'))
    plan['targets']=[{} for _ in range(index)]
    return rows,plan,events


@pytest.mark.parametrize('exit_stage',[True,False])
def test_regrasp_open_exit_clearance_close_lift_are_all_classified(exit_stage):
    rows,plan,events=regrasp_evidence(exit_stage)
    result=grasp_phases(rows,plan,events,'grasp',0,100,skill='regrasp')
    assert result['plan_completed']
    assert result['semantic_stages']==[s['semantic_stage'] for s in plan['geometry_trace'] for _ in range(s['end_index']-s['start_index'])]
    assert result['closure_row']==len(rows)-2
    assert result['phases'][-1]=='support_lift'


@pytest.mark.parametrize('cutoff',[1,2,3,4,5])
def test_regrasp_failure_prefix_preserves_all_emitted_stages(cutoff):
    rows,plan,events=regrasp_evidence();rows=rows[:cutoff]
    events=[e for e in events if e['monotonic_ns']<=rows[-1]['virtual_ns']]
    result=grasp_phases(rows,plan,events,'grasp',0,100,skill='regrasp',complete=False)
    assert not result['plan_completed']
    assert len(result['semantic_stages'])==cutoff
    assert result['phases'][0]=='regrasp_open'


@pytest.mark.parametrize('fault,reason',[
    ('contact_hold_move','arm_command_moved_during_contact_reopen'),
    ('closed_exit','regrasp_travel_requires_open_gripper|closure_before_precontact_arrival|gripper_reopened'),
    ('missing_open_arrival','grasp_stage_arrival_order_invalid'),
    ('unknown_stage','unregistered_grasp_stage_topology'),
])
def test_regrasp_prefix_cannot_bypass_registered_stage_contract(fault,reason):
    rows,plan,events=regrasp_evidence()
    if fault=='contact_hold_move':rows[0]['submitted16'][7]=.01
    elif fault=='closed_exit':rows[1]['submitted16'][15]=100
    elif fault=='missing_open_arrival':events.pop(0)
    elif fault=='unknown_stage':plan['geometry_trace'][1]['semantic_stage']='free_contact_motion'
    with pytest.raises(ValueError,match=reason):grasp_phases(rows,plan,events,'grasp',0,100,skill='regrasp',complete=False)


def test_failed_and_regrasp_outputs_are_dispatched_and_missing_plan_is_failed(monkeypatch):
    import grasp_output_sweeps as sweep
    calls=[]
    def inspect(rows,events,plan,service,numeric):
        calls.append(plan['request_id'])
        return dict(request_id=plan['request_id'],passed=True,samples=len(rows))
    monkeypatch.setattr(sweep,'_sweep_request',inspect)
    plans=[dict(skill='grasp',request_id='failed'),dict(skill='regrasp',request_id='recovery')]
    events=[dict(kind='skill_result',result=dict(request_id='failed',outcome='failure')),
            dict(kind='harness_planning_started',request_id='orphan',intent=dict(skill='regrasp'))]
    rows=[dict(request_id=request) for request in ('failed','recovery','orphan')]
    result=sweep.sweep_grasp(rows,events,plans,None,None)
    assert calls==['failed','recovery']
    assert result[2]['request_id']=='orphan' and not result[2]['passed']
    assert result[2]['covered_output_rows']==0 and result[2]['audit_error']=='grasp_plan_missing_or_ambiguous'


def test_corrupt_first_prefix_does_not_hide_later_regrasp(monkeypatch):
    import grasp_output_sweeps as sweep
    def inspect(rows,events,plan,service,numeric):
        if plan['request_id']=='failed':raise ValueError('bad_evidence')
        return dict(request_id='recovery',passed=True,samples=len(rows))
    monkeypatch.setattr(sweep,'_sweep_request',inspect)
    plans=[dict(skill='grasp',request_id='failed'),dict(skill='regrasp',request_id='recovery')]
    result=sweep.sweep_grasp([dict(request_id=p['request_id']) for p in plans],[],plans,None,None)
    assert len(result)==2 and not result[0]['passed'] and result[1]['passed']


def test_published_request_with_missing_output_is_failed_not_skipped():
    from grasp_output_sweeps import sweep_grasp
    plan=dict(skill='grasp',request_id='missing')
    events=[dict(kind='harness_skill_first_publication',request_id='missing')]
    result=sweep_grasp([],events,[plan],None,None)
    assert len(result)==1 and not result[0]['passed']
    assert result[0]['samples']==0 and result[0]['audit_error']=='published_grasp_outputs_missing'
