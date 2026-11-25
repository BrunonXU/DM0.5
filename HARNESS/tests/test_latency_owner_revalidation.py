"""Owner lifecycle with actual planning/revalidation code and synthetic geometry.

Numerics and asynchronous scheduling are controlled test doubles. No native
motion, ROS, hardware publisher or physical success is exercised here.
"""
from collections import Counter
from dataclasses import asdict
import pytest

from f1_harness.contracts import FixturePose, SkillIntent, Task, digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.owner_bridge import JointProposal, OwnerBridge
from f1_harness.revalidation import candidate_hash, revalidate_candidate
from test_full_development_skills import pose, scene
from test_plan_revalidation import observation, service
from test_unpublished_plan import ControlledExecutor


def make_loaded_owner(tmp_path, *, loaded=True, obstacle=False, skill='move_tcp'):
    clock = [1_000_000_000]
    engine = TaskEngine(EvidenceStore(tmp_path), mode='hardware',
                        clock=lambda: clock[0], lease_ms=20_000)
    task = Task(task_id='latency', object_id='block', hole_id='hole',
                calibration_id='coarse', admission_profile='development_coarse',
                allowed_skills=(skill,))
    session = engine.open_session()['session_id']
    engine.prepare_task(session, task.model_dump())
    engine.hardware_ready = True
    engine.local_start(physical_event=True, feedback_healthy=True)
    original = observation(clock[0], boot=engine.host_boot_id)
    if loaded:
        attachment = pose(0, 0, -.1)
        obj = original.objects[0].model_copy(update=dict(held=True,
            eef_object_pose=tuple(attachment.ravel()),
            pose=tuple((scene().base_eef @ attachment).ravel())))
        original = original.model_copy(update=dict(
            state16=(0.,)*15+(100.,), objects=(obj, original.objects[1])))
    if skill=='align':
        head=tuple(f.blob_sha256 for f in original.frames if f.camera=='head')
        hole=original.objects[1].model_copy(update={'evidence_ids':head})
        original=original.model_copy(update={'objects':(original.objects[0],hole)})
        engine.completed_skills.add('inspect_in_hand')
    fixture = FixturePose(fixture_id='bucket', frame_id='native',
        pose=original.objects[1].pose, reference_pose=original.objects[1].pose,
        evidence_ids=('head-fixture',), measured_at_ns=clock[0])
    original = original.model_copy(update={'fixture': fixture})
    engine.update_observation(original)
    serv = service(clock, task)
    serv.fixture.update(cartesian_motion=True, independent_fixture_localization=True,
                        fixture_reference_hole=list(fixture.reference_pose))
    serv.fixture['revalidation']['free_space_goal_tolerance_m'] = .015
    if obstacle:
        serv.fixture['obstacles'] = [dict(role='bucket', fixture_part='blocker',
            min=[-.04, -.04, -.04], max=[.04, .04, .04], pose=pose(2, 0, .5).tolist())]
        if obstacle == 'attachment':
            serv.fixture['obstacles'] = [dict(role='bucket', fixture_part='blocker',
                min=[-.008, -.008, -.008], max=[.008, .008, .008],
                pose=pose(.08, 0, .43).tolist())]
    tcp = scene().base_eef @ scene().eef_tip_reference
    tcp[2, 3] += .02
    if skill=='transfer_above_hole':engine.completed_skills.add('verify_grasp')
    intent = SkillIntent(request_id='motion', session_id=session, owner_instance_id=engine.instance,
        task_id=task.task_id, task_hash=digest(task), epoch=engine.epoch,
        observation_id=original.observation_id, scene_version=original.scene_version,
        calibration_id=task.calibration_id, skill=skill,
        parameters={'target_tcp_pose': tuple(tcp.ravel())} if skill=='move_tcp' else {})
    engine.submit_skill(intent.model_dump())
    assert engine.results['motion'].lifecycle == 'accepted'
    counts = Counter()
    actual_call = serv.numerics.call

    def numeric_call(method, **kwargs):
        purpose = ('validate_existing' if kwargs.get('scene', {}).get('joint_path_to_validate')
                   is not None else 'generate') if method == 'solve_path' else method
        counts[purpose] += 1
        return actual_call(method, **kwargs)

    serv.numerics.call = numeric_call

    def plan(intent, obs, full, held):
        counts['plan_dispatch'] += 1
        return JointProposal(**serv.dispatch('plan_skill', dict(intent=intent.model_dump(),
            observation=obs.model_dump(), full22=full, held16=held)))

    def revalidate(intent, candidate, original, latest, full, held, after):
        counts['revalidate_dispatch'] += 1
        return revalidate_candidate(serv, intent=intent.model_dump(), proposal=asdict(candidate),
            original_observation=original.model_dump(), observation=latest.model_dump(),
            full22=full, held16=held, after_ns=after)

    bridge = OwnerBridge(engine, numerical_planner=plan, candidate_revalidator=revalidate,
        result_verifier=lambda *args: None, clock=lambda: clock[0],
        feedback_age_ns=200_000_000, reference_tolerance_deg=.5, stop_gripper_delta=.1)
    bridge.executor.shutdown()
    bridge.executor = ControlledExecutor()
    state = dict(full22=[0.]*22, measured16=list(original.state16), held16=list(original.state16))

    def poll():
        return bridge.poll(**state, stamps16=(clock[0],)*16)

    return engine, bridge, serv, clock, state, poll, original, counts


def fresh(original, clock, name, *, attachment_x=None, fixture_x=None, attachment_missing=False):
    clock[0] += 20_000_000
    stamp = observation(clock[0], boot=original.host_boot_id, name=name)
    obj = original.objects[0]
    if attachment_x is not None:
        matrix = pose(attachment_x, 0, -.1)
        obj = obj.model_copy(update={'eef_object_pose': tuple(matrix.ravel())})
    if attachment_missing:
        obj = obj.model_copy(update={'eef_object_pose': None})
    fixture = original.fixture.model_copy(update=dict(measured_at_ns=clock[0], evidence_ids=(name,)))
    if fixture_x is not None:
        matrix = list(fixture.pose)
        matrix[3] += fixture_x
        fixture = fixture.model_copy(update={'pose': tuple(matrix)})
    return stamp.model_copy(update=dict(state16=original.state16,
        objects=(obj, original.objects[1]), fixture=fixture))


def ready_revalidation(case, *, measured_fixture=False):
    engine, bridge, _, clock, _, poll, original, _ = case
    assert poll() is None
    future, fn, args = bridge.executor.jobs[-1]
    candidate = fn(*args)
    clock[0] += 50_000_000
    future.set_result(candidate)
    assert poll() is None
    current = fresh(original, clock, 'first-fresh')
    if measured_fixture:
        sources=tuple(f.blob_sha256 for f in current.frames if f.camera=='head')
        fixture=current.fixture.model_copy(update={'evidence_ids':sources})
        current=current.model_copy(update={'fixture':fixture})
    engine.update_live_observation(current, engine.status()['perception_context'])
    assert poll() is None
    return candidate, bridge.executor.jobs[-1]


def test_new_attachment_pose_reuses_same_candidate_but_rechecks_every_fresh_scene(tmp_path):
    case = make_loaded_owner(tmp_path)
    engine, bridge, _, clock, state, poll, original, counts = case
    try:
        candidate, job = ready_revalidation(case)
        identity = candidate_hash(candidate)
        deadline, epoch, after = engine.deadline_ns, engine.epoch, bridge.revalidation_after_ns
        generated = counts['generate']
        for index in range(3):
            future, fn, args = job
            proof = fn(*args)
            current = fresh(original, clock, f'attachment-{index}', attachment_x=.02*(index+1))
            engine.update_live_observation(current, engine.status()['perception_context'])
            future.set_result(proof)
            assert poll() is None
            assert bridge.pending_revalidation_plan is candidate
            assert bridge.proposal is None and bridge.current_target is None
            assert bridge.revalidation_proof is None
            assert candidate_hash(candidate) == identity
            assert (engine.deadline_ns, engine.epoch, bridge.revalidation_after_ns) == (deadline, epoch, after)
            assert engine.running.request_id == 'motion' and engine.authorized
            assert counts['plan_dispatch'] == 1 and counts['generate'] == generated
            assert not engine.request_publications
            assert poll() is None
            job = bridge.executor.jobs[-1]
            assert job[2][1] is candidate and job[2][3] == current
        future, fn, args = job
        final_proof = fn(*args)
        future.set_result(final_proof)
        assert poll() is not None
        assert bridge.proposal is candidate and bridge.revalidation_proof is final_proof
        assert final_proof['geometry_checks']['swept_segments'] > 0
        assert counts['revalidate_dispatch'] == 4
        assert counts['plan_dispatch'] == 1 and counts['generate'] == generated
        assert bridge.validate_first_publication(**state, stamps16=(clock[0],)*16, now_ns=clock[0])
    finally:
        bridge.close()


@pytest.mark.parametrize('loaded', [False, True])
def test_attachment_appearance_or_disappearance_is_not_pose_only_resweep(tmp_path, loaded):
    case = make_loaded_owner(tmp_path, loaded=loaded)
    engine, bridge, _, clock, _, poll, original, counts = case
    try:
        _, (future, fn, args) = ready_revalidation(case)
        proof = fn(*args)
        current = fresh(original, clock, 'attachment-identity-change',
                        attachment_missing=loaded, attachment_x=None if loaded else 0.)
        if loaded:
            current = current.model_copy(update={'objects':(current.objects[0].model_copy(update={'held':False}),
                *current.objects[1:])})
        engine.update_live_observation(current, engine.status()['perception_context'])
        generated = counts['generate']
        future.set_result(proof)
        assert poll() is None
        assert bridge.pending_revalidation_plan is None and bridge.proposal is None
        assert 'attachment_changed' in engine.results['motion'].reason
        assert counts['plan_dispatch'] == 1 and counts['generate'] == generated
        assert not engine.request_publications
    finally:
        bridge.close()


@pytest.mark.parametrize('changed_geometry', ['fixture', 'attachment'])
def test_new_obstacle_rejects_retained_candidate_without_generating_replacement(tmp_path, changed_geometry):
    case = make_loaded_owner(tmp_path, obstacle=changed_geometry)
    engine, bridge, _, clock, _, poll, original, counts = case
    try:
        candidate, (future, fn, args) = ready_revalidation(case)
        proof = fn(*args)
        current = fresh(original, clock, 'obstacle-enters-corridor',
                        fixture_x=-2. if changed_geometry == 'fixture' else None,
                        attachment_x=.08 if changed_geometry == 'attachment' else None)
        engine.update_live_observation(current, engine.status()['perception_context'])
        future.set_result(proof)
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        generated = counts['generate']
        assert poll() is None
        future, fn, args = bridge.executor.jobs[-1]
        with pytest.raises(Exception, match='sweep_blocked') as caught:
            fn(*args)
        future.set_exception(caught.value)
        assert poll() is None
        assert bridge.proposal is None and bridge.pending_revalidation_plan is None
        assert engine.results['motion'].outcome != 'success'
        assert engine.results['motion'].no_motion
        assert counts['plan_dispatch'] == 1 and counts['generate'] == generated
        assert not engine.request_publications
    finally:
        bridge.close()


@pytest.mark.parametrize('interrupt', ['stop', 'deadline'])
def test_pending_resweep_late_future_cannot_outlive_stop_or_original_deadline(tmp_path, interrupt):
    case = make_loaded_owner(tmp_path)
    engine, bridge, _, clock, _, poll, original, counts = case
    try:
        candidate, (future, fn, args) = ready_revalidation(case)
        proof = fn(*args)
        current = fresh(original, clock, 'changed-before-completion', attachment_x=.02)
        engine.update_live_observation(current, engine.status()['perception_context'])
        future.set_result(proof)
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        deadline = engine.deadline_ns
        assert poll() is None
        future, fn, args = bridge.executor.jobs[-1]
        assert future.set_running_or_notify_cancel()
        late_proof = fn(*args)
        jobs = len(bridge.executor.jobs)
        generated = counts['generate']
        if interrupt == 'stop':
            bridge.revoke('user_stop', preempted=True)
        else:
            clock[0] = deadline
            assert poll() is None
        future.set_result(late_proof)
        for _ in range(3):
            assert poll() is None
        assert not engine.authorized
        assert bridge.proposal is None and bridge.pending_revalidation_plan is None
        assert len(bridge.executor.jobs) == jobs
        assert counts['plan_dispatch'] == 1 and counts['generate'] == generated
        assert not engine.request_publications
        assert engine.results['motion'].reason == ('user_stop' if interrupt == 'stop' else 'skill_deadline')
    finally:
        bridge.close()


@pytest.mark.parametrize('publication_count', [0, 1])
def test_unknown_or_partial_publication_marker_cannot_reenter_first_publication(tmp_path, publication_count):
    case = make_loaded_owner(tmp_path)
    engine, bridge, _, _, _, poll, _, counts = case
    try:
        _, (future, fn, args) = ready_revalidation(case)
        proof = fn(*args)
        # An output receipt may arrive before the in-memory proposal_started
        # flag. Even a zero/unknown receipt must not become "unpublished".
        engine.request_publications['motion'] = {'publication_count': publication_count}
        future.set_result(proof)
        assert poll() is None
        assert not engine.authorized
        assert bridge.proposal is None and bridge.pending_revalidation_plan is None
        assert counts['plan_dispatch'] == 1
        assert engine.results['motion'].no_motion is False
    finally:
        bridge.close()


def test_published_motion_does_not_restart_generation_or_presweep_for_new_scene(tmp_path):
    case = make_loaded_owner(tmp_path)
    engine, bridge, _, clock, state, poll, original, counts = case
    try:
        candidate, (future, fn, args) = ready_revalidation(case)
        future.set_result(fn(*args))
        assert poll() is not None
        bridge.after_publication(publication_id='actual-output-receipt', measured16=state['measured16'],
                                 publication_complete=True, now_ns=clock[0])
        assert bridge.proposal_started and engine.request_publications['motion']['publication_count'] == 1
        generated = counts['generate']
        revalidated = counts['revalidate_dispatch']
        jobs = len(bridge.executor.jobs)
        current = fresh(original, clock, 'after-publication', attachment_x=.02)
        engine.update_live_observation(current, engine.status()['perception_context'])
        assert poll() is not None
        assert bridge.proposal is candidate and bridge.pending_revalidation_plan is None
        assert counts['generate'] == generated and counts['revalidate_dispatch'] == revalidated
        assert len(bridge.executor.jobs) == jobs
        assert engine.request_publications['motion']['publication_count'] == 1
    finally:
        bridge.close()

@pytest.mark.parametrize('current_held',[None,True])
def test_missing_collision_attachment_after_worker_result_keeps_one_candidate(tmp_path,current_held):
    case=make_loaded_owner(tmp_path,loaded=True)
    engine,bridge,_,clock,state,poll,original,counts=case
    try:
        candidate,(future,fn,args)=ready_revalidation(case)
        bound_deadline=engine.deadline_ns
        proof=fn(*args)
        missing=fresh(original,clock,'missing-transform',attachment_missing=True)
        obj=missing.objects[0].model_copy(update={'held':current_held})
        missing=missing.model_copy(update={'objects':(obj,*missing.objects[1:])})
        engine.update_live_observation(missing,engine.status()['perception_context'])
        generated=counts['generate'];future.set_result(proof)
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        assert bridge.proposal is None and not engine.request_publications
        assert poll() is None and bridge.future is None
        restored=fresh(original,clock,'fresh-measured-attachment')
        engine.update_live_observation(restored,engine.status()['perception_context'])
        assert poll() is None
        future,fn,args=bridge.executor.jobs[-1];future.set_result(fn(*args))
        assert poll() is not None and bridge.proposal is candidate
        assert bridge.validate_first_publication(**state,stamps16=(clock[0],)*16,now_ns=clock[0])
        assert counts['plan_dispatch']==1 and counts['generate']==generated
        assert counts['revalidate_dispatch']==2 and engine.deadline_ns==bound_deadline
        assert not engine.request_publications
    finally:bridge.close()


@pytest.mark.parametrize('receive_during_worker', [False, True])
def test_transport_pending_measurement_retains_candidate_until_distinct_new_view(tmp_path, receive_during_worker):
    from f1_harness.revalidation import TRANSPORT_MEASUREMENT_PENDING
    case=make_loaded_owner(tmp_path, skill='transfer_above_hole')
    engine,bridge,_,clock,state,poll,original,counts=case
    try:
        candidate,(future,_,_)=ready_revalidation(case)
        deadline=engine.deadline_ns;generation=bridge.revalidation_after_ns
        next_view=fresh(original,clock,'direct-bucket')
        if receive_during_worker:
            engine.update_live_observation(next_view,engine.status()['perception_context'])
        # Plan service -> UnixClient: the exception class is lost at the RPC boundary.
        future.set_exception(RuntimeError('RevalidationEvidenceUpdated: '+TRANSPORT_MEASUREMENT_PENDING))
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        assert engine.authorized and engine.running.request_id==candidate.request_id
        generated=counts['generate'];jobs=len(bridge.executor.jobs)
        if not receive_during_worker:
            for _ in range(3):assert poll() is None
            assert len(bridge.executor.jobs)==jobs and not engine.request_publications
            engine.update_live_observation(next_view,engine.status()['perception_context'])
        assert poll() is None and len(bridge.executor.jobs)==jobs+1
        future,fn,args=bridge.executor.jobs[-1]
        assert args[1] is candidate and args[3] is next_view
        proof=fn(*args);future.set_result(proof)
        assert poll() is not None
        bridge.after_publication(publication_id='memory-output',measured16=state['measured16'],
                                 publication_complete=True,now_ns=clock[0])
        assert engine.request_publications[candidate.request_id]['publication_count']==1
        assert counts['plan_dispatch']==1 and counts['generate']==generated
        assert proof['after_planning_ns']==generation and engine.deadline_ns==deadline
    finally:bridge.close()


@pytest.mark.parametrize('cause', ['revalidation_camera_identity_changed',
    'revalidation_actual_path_sweep_blocked', 'revalidation_attachment_changed'])
def test_transport_hard_rejection_discards_route_without_publishing(tmp_path,cause):
    case=make_loaded_owner(tmp_path,skill='transfer_above_hole')
    engine,bridge,_,_,_,poll,_,counts=case
    try:
        candidate,(future,_,_)=ready_revalidation(case)
        future.set_exception(RuntimeError('HarnessError: '+cause))
        assert poll() is None and bridge.pending_revalidation_plan is None
        assert engine.results[candidate.request_id].no_motion
        assert counts['plan_dispatch']==1 and not engine.request_publications
    finally:bridge.close()


def test_transport_missing_reference_after_sweep_waits_for_new_view_before_first_output(tmp_path):
    case=make_loaded_owner(tmp_path,skill='transfer_above_hole')
    engine,bridge,_,clock,state,poll,original,counts=case
    try:
        candidate,(future,fn,args)=ready_revalidation(case,measured_fixture=True)
        proof=fn(*args)
        incomplete=fresh(original,clock,'bucket-obscured')
        incomplete=incomplete.model_copy(update={'fixture':None, 'objects':incomplete.objects[:1]})
        engine.update_live_observation(incomplete,engine.status()['perception_context'])
        deadline=engine.deadline_ns;jobs=len(bridge.executor.jobs)
        future.set_result(proof)
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        for _ in range(3):assert poll() is None
        assert len(bridge.executor.jobs)==jobs and not engine.request_publications
        restored=fresh(original,clock,'bucket-reacquired')
        engine.update_live_observation(restored,engine.status()['perception_context'])
        assert poll() is None and len(bridge.executor.jobs)==jobs+1
        future,fn,args=bridge.executor.jobs[-1]
        assert args[1] is candidate and args[3] is restored
        future.set_result(fn(*args))
        assert poll() is not None and engine.deadline_ns==deadline
        bridge.after_publication(publication_id='memory-output',measured16=state['measured16'],
                                 publication_complete=True,now_ns=clock[0])
        assert counts['plan_dispatch']==1 and engine.request_publications[candidate.request_id]['publication_count']==1
    finally:bridge.close()


@pytest.mark.parametrize('termination',['feedback_invalidated','deadline'])
def test_waiting_transport_stops_on_hard_feedback_fault_or_original_deadline(tmp_path,termination):
    from f1_harness.revalidation import TRANSPORT_MEASUREMENT_PENDING
    case=make_loaded_owner(tmp_path,skill='transfer_above_hole')
    engine,bridge,_,clock,_,poll,_,counts=case
    try:
        candidate,(future,_,_)=ready_revalidation(case)
        future.set_exception(RuntimeError('RevalidationEvidenceUpdated: '+TRANSPORT_MEASUREMENT_PENDING))
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        if termination=='deadline':clock[0]=engine.deadline_ns
        else:engine.invalidate_live_observation('feedback_expired:right')
        assert poll() is None and not engine.authorized and bridge.pending_revalidation_plan is None
        assert engine.results[candidate.request_id].reason==(
            'skill_deadline' if termination=='deadline' else 'revalidation_live_scene_invalidated')
        assert counts['plan_dispatch']==1 and not engine.request_publications
    finally:bridge.close()


@pytest.mark.parametrize('next_event', ['fresh_pair', 'controller_feedback_lost'])
def test_held_viewpoint_waits_for_camera_pair_while_owner_checks_live_joints(tmp_path, next_event):
    case = make_loaded_owner(tmp_path)
    engine, bridge, _, clock, state, poll, original, counts = case
    try:
        candidate, (worker, validate, args) = ready_revalidation(case)
        proof = validate(*args)
        deadline, epoch = engine.deadline_ns, engine.epoch
        generated = counts['generate']
        engine.invalidate_live_observation('camera_sample_feedback_unpaired:right')
        worker.set_result(proof)
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        assert bridge.current_target is None and bridge.revalidation_proof is None
        assert engine.authorized and not engine.request_publications
        assert poll() is None and bridge.future is None

        if next_event == 'controller_feedback_lost':
            stamps = [clock[0]] * 16
            stamps[7] -= bridge.feedback_age_ns + 1
            assert bridge.poll(**state, stamps16=stamps) is None
            assert not engine.authorized and bridge.pending_revalidation_plan is None
            assert engine.results[candidate.request_id].reason == 'harness_feedback_stale'
            assert not engine.request_publications
            return

        paired = fresh(original, clock, 'new-wrist-pair')
        engine.update_live_observation(paired, engine.status()['perception_context'])
        assert poll() is None
        worker, validate, args = bridge.executor.jobs[-1]
        assert args[1] is candidate and args[3] is paired
        worker.set_result(validate(*args))
        assert poll() is not None and bridge.proposal is candidate
        bridge.after_publication(publication_id='memory-viewpoint',
                                 measured16=state['measured16'], publication_complete=True, now_ns=clock[0])
        assert engine.request_publications[candidate.request_id]['publication_count'] == 1
        assert (engine.deadline_ns, engine.epoch) == (deadline, epoch)
        assert counts['plan_dispatch'] == 1 and counts['generate'] == generated
        assert counts['revalidate_dispatch'] == 2
    finally:
        bridge.close()

@pytest.mark.parametrize('following', ['restored', 'expired', 'dropped'])
def test_align_retains_original_candidate_until_post_planning_aperture_is_measured(tmp_path, following):
    case=make_loaded_owner(tmp_path,skill='align')
    engine,bridge,_,clock,state,poll,original,counts=case
    try:
        assert poll() is None
        future,fn,args=bridge.executor.jobs[-1]
        candidate=fn(*args)
        clock[0]+=50_000_000
        future.set_result(candidate)
        assert poll() is None
        missing=fresh(original,clock,'duplicate-head')
        missing=missing.model_copy(update={'objects':missing.objects[:1],
            'quality_warnings':('fixture_unavailable:capture_faults:duplicate_source_stamp:head',)})
        engine.update_live_observation(missing,engine.status()['perception_context'])
        jobs=len(bridge.executor.jobs)
        assert poll() is None and bridge.pending_revalidation_plan is candidate
        assert len(bridge.executor.jobs)==jobs and not engine.request_publications
        current=fresh(original,clock,'new-full-measurement')
        if following=='expired':
            clock[0]+=4_000_000_000
            assert poll() is None
            assert engine.results['motion'].reason.endswith('revalidation_target_measurement_unavailable')
        elif following=='dropped':
            detached=current.objects[0].model_copy(update={'held':False,'eef_object_pose':None})
            current=current.model_copy(update={'objects':(detached,current.objects[1])})
            engine.update_live_observation(current,engine.status()['perception_context'])
            assert poll() is None and engine.results['motion'].reason.endswith('revalidation_attachment_changed')
        else:
            engine.update_live_observation(current,engine.status()['perception_context'])
            assert poll() is None and len(bridge.executor.jobs)==jobs+1
            future,fn,args=bridge.executor.jobs[-1]
            assert args[1] is candidate and args[2] is original and args[3] is current
            future.set_result(fn(*args))
            assert poll() is not None
            bridge.after_publication(publication_id='memory-align',measured16=state['measured16'],
                publication_complete=True,now_ns=clock[0])
            assert engine.request_publications['motion']['publication_count']==1
        if following!='restored':
            assert not engine.request_publications and bridge.pending_revalidation_plan is None
        assert counts['plan_dispatch']==1
    finally:bridge.close()

def test_reobserve_snapshot_id_crosses_real_http_and_owner_ipc_only_with_publication_revalidator(tmp_path):
    from types import SimpleNamespace
    from rpent.utils.rpc.http_rpc import HttpRpcClient
    from f1_harness.gateway import Gateway, GatewayServer
    from f1_harness.remote import OwnerProxy
    engine, bridge, _, _, _, _, original, _ = make_loaded_owner(tmp_path,skill='align')
    bridge.serve(tmp_path/'owner.sock')
    gateway=GatewayServer(Gateway(OwnerProxy(tmp_path/'owner.sock'),SimpleNamespace(observe=lambda:original),engine.store)).start()
    rpc=HttpRpcClient(gateway.url,enable_sessions=False)
    try:
        measured=rpc.call('observe_with_status',kwargs={},timeout_s=2)
        assert measured['status']['alignment_snapshot_id']==measured['observation']['observation_id']
        assert rpc.call('status',kwargs={},timeout_s=2)['alignment_snapshot_id']==original.observation_id
        bridge.candidate_revalidator=None
        assert 'alignment_snapshot_id' not in rpc.call('status',kwargs={},timeout_s=2)
    finally:
        gateway.close()
        bridge.close()
