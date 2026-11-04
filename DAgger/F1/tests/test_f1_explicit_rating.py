"""V4 rating edges, ownership and immutable local outcomes."""
import time
from dagger_core.hil_supervisor import HILState
from test_f1_rlt_hil import make_engine, switch, send_operator


def test_unrated_y_and_handoff_rejected_without_ending_human_segment():
    e, j, _, _ = make_engine()
    try:
        switch(e)
        send_operator(e, 0)
        send_operator(e, 1, home_end_id=1, home_reset=True)
        assert e.local_task.status == 'active' and not e.home_request
        send_operator(e, 2, 1, home_end_id=1, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
        intervention = e.supervisor.intervention_id
        send_operator(e, 3, 1, home_end_id=1)
        epoch = e.supervisor.policy_epoch
        send_operator(e, 4, 2, home_end_id=1, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert e.supervisor.policy_epoch == epoch
        assert e.supervisor.intervention_id == intervention
        assert not any(n == ('hil_to_handoff_hold',) for n, _ in j.events)
        send_operator(e, 5, 2, home_end_id=1)
        send_operator(e, 6, 2, home_end_id=1, episode_end_id=1,
                      episode_end_pressed=True, local_result='failure')
        assert e.local_task.status == 'failure'
        assert e.supervisor.state is HILState.HIL_LATCHED
        # Repeated physical packet cannot duplicate reward or alter outcome.
        send_operator(e, 6, 2, home_end_id=1, episode_end_id=1,
                      episode_end_pressed=True, local_result='failure')
        assert sum(n == ('rlt_local_closed',) for n, _ in j.events) == 1
        send_operator(e, 7, 2, home_end_id=1, episode_end_id=1)
        send_operator(e, 8, 3, home_end_id=1, episode_end_id=1, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HANDOFF_HOLD and e.rlt_phase == 'base'
    finally:
        e.close()


def test_failure_stops_autonomy_without_auto_human_or_auto_home():
    e, j, _, _ = make_engine()
    try:
        switch(e)
        send_operator(e, 0)
        send_operator(e, 1, episode_end_id=1, episode_end_pressed=True, local_result='failure')
        assert e.local_task.status == 'failure' and not e.policy_enabled
        assert e.supervisor.state is HILState.HANDOFF_HOLD and not e.home_request
        assert not e.local_success(time.monotonic_ns())
        send_operator(e, 2, episode_end_id=1)
        send_operator(e, 3, 1, episode_end_id=1, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
    finally:
        e.close()


def test_unrated_shutdown_retains_unknown_outcome():
    e, j, _, _ = make_engine()
    switch(e)
    e.close()
    result = next(d for n, d in j.events if n == ('rlt_local_closed',))
    assert result['status'] == 'interrupted' and result['reward'] is None
