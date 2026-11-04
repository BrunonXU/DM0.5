"""Capability checks cannot mislabel checkpoint or recovery deployment."""
import pytest
from start_f1_inference import verify_profile


def health(model='any-checkpoint'):
    return dict(ready=True,protocol='f1_eef_rtc_v2',model_id=model,model_version='version',
        ik_recovery=dict(enabled=True,wrapper='f1_strict_ik_recovery_v1',eef_postprocessed=False,
            ranges_changed=False,may_select_resampled_prediction=True,
            partial_response='standard_ik_rejection_requires_opt_in_client',
            wrapper_sha256='sha',max_attempts=4,budget_ms=400.))


@pytest.mark.parametrize('model',['lora50k','full1500','future-checkpoint'])
def test_recovery_contract_has_no_checkpoint_branch(model):
    result=verify_profile(dict(model_id=model,recovery='ik',client_tracking_hold=True,client_version='r3'),health(model))
    assert result['server_resampling'] and result['client_tracking_hold']


@pytest.mark.parametrize('fault',['wrong_model','missing_recovery','modified_eef','modified_ranges','standard_on_recovery','unverified_client'])
def test_no_silent_mislabel_or_recovery_fallback(fault):
    profile=dict(model_id='any-checkpoint',recovery='ik',client_tracking_hold=True,client_version='r3');h=health()
    if fault=='wrong_model':h['model_id']='different'
    if fault=='missing_recovery':h.pop('ik_recovery')
    if fault=='modified_eef':h['ik_recovery']['eef_postprocessed']=True
    if fault=='modified_ranges':h['ik_recovery']['ranges_changed']=True
    if fault=='standard_on_recovery':profile['recovery']='standard'
    if fault=='unverified_client':profile['client_tracking_hold']=False
    with pytest.raises(ValueError):verify_profile(profile,h)


def test_standard_reports_existing_capabilities_only():
    h=health();h.pop('ik_recovery')
    result=verify_profile(dict(model_id=h['model_id'],recovery='standard',client_version='v6'),h)
    assert not result['server_resampling'] and not result['client_feasible_prefix'] and not result['client_tracking_hold']
