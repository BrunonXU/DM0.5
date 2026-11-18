"""Only confirmed zero-publication temporal rejection permits a newly bound request."""
from dataclasses import replace

import pytest

from f1_harness.contracts import HarnessError, SkillResult, digest
from f1_harness.engine import UNPUBLISHED_FRESHNESS_REASONS
from f1_harness.gateway import OWNER_METHODS
from test_unpublished_plan import finish_plan, make_bridge


def fresh_observation(original, now):
    return original.model_copy(update=dict(observation_id='new-observation', scene_version='new-scene',
        received_ns=now, state_received_ns=now,
        frames=tuple(frame.model_copy(update=dict(source_ns=now, received_ns=now,
                                                  sequence=frame.sequence+1)) for frame in original.frames)))


@pytest.mark.parametrize('skill', ['approach_grasp', 'transfer_above_hole'])
def test_expired_candidate_replans_with_new_identity_and_old_request_remains_terminal(tmp_path, skill):
    engine, bridge, clock, state, poll, original = make_bridge(tmp_path)
    old=engine.pending.model_copy(update={'skill':skill})
    engine.pending=old;engine.request_hashes[old.request_id]=digest(old)
    if skill=='transfer_above_hole':engine.completed_skills.update(('verify_grasp','inspect_in_hand'))
    epoch=engine.epoch;session=engine.session
    try:
        assert poll() is None
        finish_plan(bridge);clock[0]+=600_000_000
        assert poll() is None
        rejected=engine.results[old.request_id].model_dump()
        assert rejected['no_motion'] and rejected['retryable']
        assert engine.authorized and engine.epoch==epoch and engine.session==session
        assert not engine.hold_requested and skill not in engine.attempted_motion
        assert bridge.future is None and bridge.future_request is None
        assert bridge.proposal is None and bridge.current_target is None
        # Idempotency is unchanged; retransmitting the old request cannot move.
        assert engine.submit_skill(old.model_dump())==rejected
        assert engine.pending is None
        latest=fresh_observation(original,clock[0]);engine.update_observation(latest)
        new=old.model_copy(update={'request_id':'new-request','observation_id':latest.observation_id,
                                   'scene_version':latest.scene_version})
        engine.submit_skill(new.model_dump());assert poll() is None
        finish_plan(bridge);target=poll();assert target is not None
        bridge.after_publication(publication_id='new-first',measured16=state['measured16'],
            publication_complete=True,now_ns=clock[0])
        assert engine.request_publications[new.request_id]['first_publication_id']=='new-first'
        assert engine.request_publications[new.request_id]['publication_count']==1
        assert old.request_id not in engine.request_publications
        assert engine.results[old.request_id].model_dump()==rejected
        assert engine.task_requests==2
    finally:bridge.close()


def test_any_complete_publication_disallows_zero_motion_retry(tmp_path):
    engine, bridge, clock, state, poll, _=make_bridge(tmp_path)
    try:
        poll();finish_plan(bridge);assert poll() is not None
        bridge.after_publication(publication_id='physical-first',measured16=state['measured16'],
            publication_complete=True,now_ns=clock[0])
        assert engine.request_publications['one-request']['publication_count']==1
        with pytest.raises(HarnessError,match='unpublished_replan_not_eligible'):
            engine.complete('one-request',SkillResult(request_id='one-request',lifecycle='completed',
                outcome='failure',reason='planning_observation_expired_before_publication',
                no_motion=True,retryable=True))
        bridge._discard_unpublished_plan('planning_observation_expired_before_publication',inputs_unchanged=True)
        result=engine.results['one-request']
        assert not result.no_motion and not result.retryable
        assert not engine.authorized and engine.hold_requested
        assert 'approach_grasp' in engine.attempted_motion
    finally:bridge.close()


@pytest.mark.parametrize('fault', ['arm', 'gripper', 'body', 'feedback', 'nan_target', 'inactive_arm'])
def test_expiry_cannot_hide_concurrent_physical_or_numeric_failure(tmp_path, fault):
    engine, bridge, clock, state, poll, _=make_bridge(tmp_path)
    try:
        poll();finish_plan(bridge)
        if fault=='arm':state['full22'][7]=.6
        elif fault=='gripper':state['measured16'][15]=19.
        elif fault=='body':state['full22'][14]=.6
        elif fault in ('nan_target','inactive_arm'):
            future=bridge.executor.jobs[-1][0]
            proposal=future.result();target=list(proposal.targets[0])
            target[7 if fault=='nan_target' else 0]=float('nan') if fault=='nan_target' else 1.
            replacement=replace(proposal,targets=(tuple(target),))
            # Install through the same completed-future path, not a publisher.
            from concurrent.futures import Future
            bridge.future=Future();bridge.future.set_result(replacement)
        clock[0]+=600_000_000
        if fault=='feedback':
            stamps=[clock[0]]*16;stamps[15]-=201_000_000
            assert bridge.poll(**state,stamps16=stamps) is None
        else:assert poll() is None
        result=engine.results['one-request']
        assert not result.retryable and result.no_motion
        assert not engine.authorized and engine.hold_requested
        assert result.reason not in UNPUBLISHED_FRESHNESS_REASONS
    finally:bridge.close()


@pytest.mark.parametrize('skill', ['grasp', 'release', 'insert', 'align', 'regrasp','retreat'])
def test_zero_publication_retry_restores_semantic_counters(tmp_path, skill):
    engine, bridge, clock, _, poll, _=make_bridge(tmp_path)
    engine.pending=engine.pending.model_copy(update={'skill':skill})
    engine.needs_correction=True
    counters=(engine.corrections,engine.regrasps,engine.needs_correction,engine.last_failure)
    try:
        poll();finish_plan(bridge);clock[0]+=600_000_000
        assert poll() is None
        result=engine.results['one-request']
        assert result.retryable and result.no_motion and engine.authorized
        assert skill not in engine.attempted_motion
        assert (engine.corrections,engine.regrasps,engine.needs_correction,engine.last_failure)==counters
        assert not engine.request_publications
    finally:bridge.close()


@pytest.mark.parametrize('fault', ['cancel', 'lease', 'deadline'])
def test_terminal_authority_change_cannot_be_recovered_by_expired_plan(tmp_path, fault):
    engine, bridge, clock, _, poll, _=make_bridge(tmp_path)
    try:
        poll();future=bridge.executor.jobs[-1][0]
        assert future.set_running_or_notify_cancel()
        if fault=='cancel':engine.cancel_skill(engine.session,'one-request')
        elif fault=='lease':engine.lease_ns=100_000_000
        else:engine.deadline_ns=clock[0]+100_000_000
        clock[0]+=600_000_000;finish_plan(bridge)
        assert poll() is None
        result=engine.results['one-request']
        assert result.lifecycle=='cancelled' and not result.no_motion and not result.retryable
        assert not engine.authorized and not engine.request_publications
    finally:bridge.close()


def test_mixed_revalidation_or_identity_reasons_are_never_retryable(tmp_path):
    engine, bridge, _, _, poll, _=make_bridge(tmp_path)
    try:
        poll()
        for reason in ('revalidation_proof_measurement_mismatch','revalidation_live_scene_invalidated',
                       'plan_revalidation_rejected:post_planning_measurements_required',
                       'revalidation_actual_path_sweep_blocked'):
            with pytest.raises(HarnessError,match='unpublished_replan_not_eligible'):
                engine.reject_unpublished_for_replan('one-request',reason=reason)
        assert engine.running is not None
        assert 'reject_unpublished_for_replan' not in OWNER_METHODS
        assert 'record_skill_publication' not in OWNER_METHODS
    finally:bridge.close()


def test_prior_skill_publication_does_not_poison_new_request_zero_motion_proof(tmp_path):
    engine, bridge, clock, _, poll, _=make_bridge(tmp_path)
    engine.request_publications['prior-skill']=dict(first_publication_id='prior',publication_count=20)
    bridge.proposal_started=True  # The preceding completed skill had published.
    try:
        poll();assert not bridge.proposal_started
        finish_plan(bridge);clock[0]+=600_000_000;assert poll() is None
        assert engine.results['one-request'].retryable and engine.authorized
        assert engine.request_publications['prior-skill']['publication_count']==20
    finally:bridge.close()
