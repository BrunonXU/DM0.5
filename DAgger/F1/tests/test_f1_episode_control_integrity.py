"""Regression for the real single-gripper episode that silently lost all controls."""
from dataclasses import replace
import json
import time

import numpy as np
import pytest

from f1dagger.demonstration import DemonstrationRecorder
from f1dagger.hardware import LEFT_GRIPPER_REMOVED, state_mask
from f1dagger.recording import audit
from f1dagger.robot_io import F1Owner
from test_f1_demonstration import wait_for
from test_f1_output import make_owner
from test_f1_policy import observation


@pytest.mark.parametrize('missing', [False, True])
def test_two_episodes_anchor_only_present_hardware_and_keep_control_evidence(tmp_path, missing):
    binding = dict(execution_mode='powered', purpose='policy_hil', auto_video=False)
    if missing:
        binding.update(hardware_capabilities=LEFT_GRIPPER_REMOVED,
                       state_valid_mask=list(state_mask(dict(hardware_capabilities=LEFT_GRIPPER_REMOVED))))
    mask = state_mask(binding)
    r = DemonstrationRecorder(tmp_path/'data', tmp_path/'runtime', binding, 'test')
    try:
        for _ in range(2):
            assert r.start(time.monotonic_ns())
            wait_for(lambda: r.phase == 'recording')
            obs = observation(0, r.begin_ns + 1)
            if missing:
                obs = replace(obs, state16=obs.state16[:14]+(0, obs.state16[15]),
                    state_timestamps_ns=obs.state_timestamps_ns[:14]+(0, obs.state_timestamps_ns[15]),
                    state_valid_mask=mask)
            assert r.record_timestamped_observation(obs, hil_state='hil_latched', intervention_id=1, policy_epoch=1)
            owner, transport = make_owner()
            transport.rmw_gids = {k: 'aa'*16 for k in transport.rmw_gids}
            owner.source.state_valid_mask = mask
            owner.source.feedback = lambda: (np.array(obs.full22), np.array(obs.state16), obs.state_timestamps_ns)
            owner = F1Owner(transport, owner.source, owner.mapping, owner.output, owner.gate, powered=True)
            now = obs.available_monotonic_ns / 1e9 + .001
            owner.initialize(now); owner.prepare()
            command, measured, evidence = owner.publish(now + .001)
            evidence = {k: v for k, v in evidence.items() if k != 'command_sequence'}
            row = dict(now=time.monotonic_ns(), state='hil_latched', intervention=1, epoch=1,
                selected=command, submitted=command, measured=measured, source='hold', sequence=owner.sequence, **evidence)
            # An opening race sees only joints; it must still wait for the new anchor.
            assert r.control(**dict(row, publications=evidence['publications'][:1]))
            assert r.first_control
            assert r.control(**row)
            assert not r.first_control
            # Ordinary ticks need not re-publish held grippers.
            command, measured, evidence = owner.publish(now + .003)
            evidence = {k: v for k, v in evidence.items() if k != 'command_sequence'}
            assert r.control(**dict(row, now=time.monotonic_ns(), sequence=owner.sequence,
                                    submitted=command, measured=measured, **evidence))
            r.finish(time.monotonic_ns())
            wait_for(lambda: r.phase in ('idle', 'failed'))
            assert r.phase == 'idle', r.error
            run = tmp_path/'data'/r.run_id
            result = audit(run)
            assert result['valid'], result
            assert result['counts']['control'] == 2
            rows = [json.loads(s) for s in (run/'journal/control.jsonl').read_text().splitlines()]
            assert {p['topic_key'] for p in rows[0]['publications']} == r.anchor_topics
            assert len(rows[1]['publications']) == 1
    finally:
        assert r.close()


def test_powered_missing_controls_preserves_raw_but_fails_save_and_audit(tmp_path):
    r = DemonstrationRecorder(tmp_path/'data', tmp_path/'runtime',
        dict(execution_mode='powered', purpose='policy_hil', auto_video=False), 'test')
    try:
        r.start(time.monotonic_ns()); wait_for(lambda: r.phase == 'recording')
        assert r.record_timestamped_observation(observation(0, r.begin_ns+1),
            hil_state='hil_latched', intervention_id=1, policy_epoch=1)
        r.finish(time.monotonic_ns()); wait_for(lambda: r.phase == 'failed')
        run = tmp_path/'data'/r.run_id
        manifest = json.loads((run/'journal/manifest.json').read_text())
        assert not manifest['complete'] and manifest['status'] == 'failed'
        assert manifest['counts']['observations'] == 1
        assert (run/'payload/manifest.json').exists()
        completion = json.loads((tmp_path/'runtime/last-session.json').read_text())
        assert completion['phase'] == 'failed' and not completion['training_ready']
        assert 'no control records' in completion['errors'][0]
        assert not audit(run)['valid']
    finally:
        assert not r.close()


def test_unsubmitted_control_cannot_unlock_the_episode_anchor(tmp_path):
    r = DemonstrationRecorder(tmp_path/'data', tmp_path/'runtime', {}, 'test')
    try:
        r.start(time.monotonic_ns()); wait_for(lambda: r.phase == 'recording')
        row = dict(now=time.monotonic_ns(), state='hil_latched', intervention=1, epoch=1,
            selected=np.zeros(16), submitted=None, measured=np.zeros(16), source='hold', sequence=0)
        assert r.control(**row)
        assert r.first_control
        assert r.control(**dict(row, submitted=np.zeros(16), publications=[dict(topic_key='joint_command')]))
        assert r.first_control
    finally:
        assert r.close()
