"""Synthetic contact outcomes; hardware capture separately validates the reader."""
from copy import deepcopy

import numpy as np
import pytest

from f1_harness.contracts import MeasuredGripperFeedback
from f1_harness.gripper_contact import blocked_closure
from test_full_development_scene import full_scene, sample


def packet(*, raw=70, end=600_000_000, source='external-1'):
    rows=[]
    for i in range(41):
        stamp=end-400_000_000+i*10_000_000
        data=bytes([0,0,raw,0,2,0,0,0])
        rows.append(dict(source_instance=source,sequence=i+1,received_ns=stamp,
            kernel_received_realtime_ns=stamp+1_000_000_000,queue_delay_ns=0,time_valid=True,
            frame_format='can_fd',can_fd_flags=0,raw_position=raw,
            closure_percent=(255-raw)*100/255,velocity_raw=0,current_raw=2,data_hex=data.hex()))
    return dict(schema='f1-gripper-feedback-v1',source='socketcan_external',
        protocol='omnipicker_status_v1',side='right',interface='can1',response_id=769,
        source_instance=source,boot_id='boot',clock_domain='host_monotonic',
        position_unit='closure_0_open_100_closed',position_resolution=100/255,
        running=True,error=None,received_count=len(rows),rejected_count=0,latest=rows[-1],samples=rows)


def evaluate(value, **kwargs):
    params=dict(frame_ns=600_000_000,after_ns=100_000_000,target=100.,open_max=18.5,
        window_ns=300_000_000,tolerance=.5,max_age_ns=200_000_000,boot_id='boot')
    return blocked_closure(value,**(params|kwargs))


def test_external_stable_blockage_accepts_without_any_visual_lift():
    result=evaluate(MeasuredGripperFeedback.model_validate(packet()))
    assert result['accepted'] and result['duration_ns']==300_000_000
    assert result['command_target']==100 and result['measured_closure']<100


@pytest.mark.parametrize('case',['missing','echo','open','empty_closed','stale','future',
    'queued','preclosure','wrong_boot','wrong_side','stopped','duplicate','gap','moving'])
def test_stability_does_not_promote_noncontact_or_invalid_feedback(case):
    value=packet();args={}
    if case=='missing':value=None
    if case=='echo':value['source']='command_echo'
    if case=='open':value=packet(raw=255)
    if case=='empty_closed':value=packet(raw=0)
    if case=='stale':args['frame_ns']=900_000_000
    if case=='future':args['frame_ns']=150_000_000
    if case=='queued':
        for row in value['samples']:row['queue_delay_ns']=500_000_000
    if case=='preclosure':args['after_ns']=400_000_000
    if case=='wrong_boot':args['boot_id']='other'
    if case=='wrong_side':value['side']='left'
    if case=='stopped':value['running']=False
    if case=='duplicate':value['samples'][-1]['sequence']=1
    if case=='gap':value['samples']=value['samples'][:1]+value['samples'][-10:]
    if case=='moving':value['samples'][-1]['closure_percent']+=2
    assert not evaluate(value,**args)['accepted']


def configured():
    pipe,raw=full_scene()
    pipe.config.update(measured_gripper_contact=True,measured_gripper_max_age_ns=200_000_000,
        stationary_window_ns=300_000_000,gripper_open_range=[17.5,18.5],
        search_object_box=dict(min=[-.015,-.015,0],max=[.015,.015,.06]))
    pipe.previous_context=dict(authorized=True,grasp_contact=dict(request_id='close',
        object_id='block',published_ns=100_000_000,target_closure=100.,
        expected_eef_object=np.eye(4).ravel().tolist()))
    return pipe,raw


def observe(pipe,raw,index=6,*,value=None,relative=None,command=100):
    obs=sample(raw,index);state=list(obs.state16);state[15]=command
    obs=obs.model_copy(update=dict(state16=tuple(state),host_boot_id='boot',
        measured_gripper=MeasuredGripperFeedback.model_validate(value or packet(end=index*100_000_000))))
    pipe.last_timing={}
    return pipe._primary_evidence(obs,np.eye(4) if relative is None else relative,
        np.eye(4),(index*100_000_000,)*2,camera='right')[-1]


def test_pipeline_contact_fast_path_reaches_held_without_moving_or_waiting_for_three_images():
    pipe,raw=configured()
    try:
        assert observe(pipe,raw) is True
        proof=pipe.last_timing['primary_evidence']
        assert proof['tracking_samples']==1 and proof['reason']=='measured_blocked_closure'
        assert proof['attachment_profile']=='independent_gripper_contact'
    finally:pipe.close()


@pytest.mark.parametrize('case',['sensor_lost','opened','empty','source_changed','object_departed','bad_image'])
def test_contact_cannot_be_latched_or_resurrected_after_contradiction(case):
    pipe,raw=configured()
    try:
        assert observe(pipe,raw) is True
        value=packet(end=700_000_000);relative=np.eye(4);command=100
        if case=='sensor_lost':value['running']=False
        if case=='opened':value=packet(raw=255,end=700_000_000)  # command echo still says 100
        if case=='empty':value=packet(raw=0,end=700_000_000)
        if case=='source_changed':value=packet(end=700_000_000,source='new')
        if case=='object_departed':relative[0,3]=.04
        if case=='bad_image':pipe.reject_measurement('mask_unavailable')
        assert observe(pipe,raw,7,value=value,relative=relative,command=command) is not True
        assert pipe.in_hand_quarantine is None
        assert observe(pipe,raw,8) is not True
    finally:pipe.close()


def test_wrong_target_region_or_unpublished_close_uses_existing_visual_path():
    pipe,raw=configured()
    try:
        far=np.eye(4);far[0,3]=.1
        assert observe(pipe,raw,relative=far) is not True
        assert pipe.last_timing['primary_evidence']['measured_contact']['reason']=='visible_target_outside_published_closure_region'
        pipe.previous_context={}
        assert observe(pipe,raw,7) is not True
    finally:pipe.close()
