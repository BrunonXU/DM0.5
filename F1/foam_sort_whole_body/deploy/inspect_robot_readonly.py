"""Migration diagnostics while other controllers may be active; never publishes."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from f1_policy_client.config import load_config
from f1_policy_client.observation import capture_observation
from f1_policy_client.policy import HttpPolicy
from f1_policy_client.ros import RosObservationBackend

ROOT = Path('/home/user/Bruno/F1/foam_sort_whole_body')
TASK = (ROOT/'current').resolve()
doc, profile = load_config(TASK/'configs/inference.f1-29.5k.json')


class ReadOnly(RosObservationBackend):
    def enable_commands(self):
        raise RuntimeError('read-only migration probe')

    def publish(self, action):
        raise RuntimeError('read-only migration probe')

    def stop_base(self):
        raise RuntimeError('read-only migration probe')

    def hold_arms(self):
        raise RuntimeError('read-only migration probe')


backend = ReadOnly(profile, doc['client']['camera_topics'])
result = {'robot': '4F-F1', 'utc': datetime.now(timezone.utc).isoformat(),
          'hardware_published': False, 'errors': {}, 'health': {}}
try:
    for port in (7892, 7894):
        try:
            result['health'][str(port)] = HttpPolicy(f'http://127.0.0.1:{port}', 10).health()
        except Exception as exc:
            result['errors'][str(port)] = str(exc)
    backend.open()
    until = time.monotonic()+4
    while time.monotonic() < until:
        backend.poll(.02)
    result['peers'] = backend.topic_peers(list(profile['topics'].values()))
    result['command_activity'] = backend.command_activity()
    try:
        result['controller'] = backend.read_controller_parameters(timeout_sec=5)
    except Exception as exc:
        result['errors']['controller'] = str(exc)
        result['controller_error_details'] = getattr(exc, 'diagnostics', None)
    try:
        result['base_parameters'] = backend.verify_base_parameters()
    except Exception as exc:
        result['errors']['base_parameters'] = str(exc)
    backend.poll(.01)
    snapshot = backend.snapshot()
    # No arbitrary camera bytes in JSON; retain timing/shape metadata only.
    result['feedback'] = {k:v for k,v in snapshot.items() if k != 'cameras'}
    result['camera_metadata'] = {k:{key:value for key,value in v.items() if key != 'data'}
                                  for k,v in snapshot.get('cameras',{}).items()}
    try:
        observation = capture_observation(snapshot, now_ns=time.monotonic_ns(),
            feedback_max_age_s=profile['feedback_max_age_s'],
            camera_max_age_s=doc['client']['camera_max_age_s'],
            camera_max_spread_s=doc['client']['camera_max_spread_s'])
        result['state19'] = list(observation.state19)
        result['body5'] = list(observation.body5)
    except Exception as exc:
        result['errors']['observation'] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    backend.close()
