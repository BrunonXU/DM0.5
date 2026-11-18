"""Bounded actual-model/native-preview dialogue, without robot or authorization.

Saved calibrated geometry is immutable. All model alternatives are validated by
the production MotionParameters and PlanService in the isolated numerics env.
This does not stand in for the full PyRUA/owner execution loop.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from f1_harness.contracts import MotionParameters
from f1_harness.evidence import EvidenceStore
from f1_harness.planner import ResponsesPlanner


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--site', type=Path, required=True)
    p.add_argument('--scene', type=Path, required=True)
    p.add_argument('--geometry', type=Path, required=True)
    p.add_argument('--failed-report', type=Path, required=True)
    p.add_argument('--images', type=Path, nargs='+', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--prior-dialogue', type=Path)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    store = EvidenceStore(a.output)
    geometry = json.loads(a.geometry.read_text())
    report = json.loads(a.failed_report.read_text())
    failure = report['stages'][0]['result']['planning_diagnostic']
    content = [dict(type='input_text', text=json.dumps(dict(
        task='Find a feasible approach and grasp on this saved scene. Default approach failed; no motion occurred.',
        geometry=geometry, failure=failure), separators=(',', ':')))]
    if a.prior_dialogue:
        prior=json.loads(a.prior_dialogue.read_text())
        content.append(dict(type='input_text',text=json.dumps(dict(previous_attempts=[
            dict(request=row['request'],failure=row['result'].get('planning_diagnostic',{}).get('metrics',{}))
            for row in prior['previews']]),separators=(',',':'))))
    for path in a.images:
        content += [dict(type='input_text', text='Camera image: ' + path.stem),
            dict(type='input_image', image_url='data:image/png;base64,' +
                 base64.b64encode(path.read_bytes()).decode())]
    inputs = [dict(role='user', content=content)]
    tool = dict(type='function', name='preview_skill', strict=False,
        description='Native IK, grasp continuation and collision preview only; no execution.',
        parameters=dict(type='object', properties=dict(
            name=dict(type='string', enum=['approach_grasp', 'move_tcp']),
            parameters=MotionParameters.model_json_schema()), required=['name','parameters'],
            additionalProperties=False))
    instructions = (
        'Use measured geometry and collision diagnostics to propose a DIFFERENT feasible grasp. '
        'All poses are row-major SE3, metres. grasp_tcp_pose is T_object_tcp; '
        'approach_direction_object points from grasp to pregrasp in object coordinates; '
        'approach_distance_m controls that displacement. These continuous parameters may change '
        'yaw/roll/pitch/contact point and cannot mix with legacy pitch_offset_deg or variant. '
        'Legacy pitch_offset_deg and variant remain convenient options, not the full action space. '
        'move_tcp takes target_tcp_pose and optional via_tcp_poses in target_frame. '
        'Do not alter measurements, obstacle dimensions, calibration or protections. '
        'tool_geometry provides the registered tool envelope relative to TCP; avoid obstacles with '
        'the whole envelope, not just the TCP point. Failed preview collision_location distinguishes '
        'start_pose, end_pose and sweep_envelope. Segment TCP poses use the same native world as geometry. '
        'Do not repeat failed parameters. You have at most 3 model rounds and 6 previews in this '
        'offline probe. Prefer a short direct grasp away from the bucket. '
        'Use preview_skill to check your choice. A successful preview is not physical success.')
    planner = ResponsesPlanner(root / 'API.MD', store)
    rows, seen, selected = [], {}, None
    model_rounds = 0
    try:
        for turn in range(3):
            response = planner.request(input=inputs, tools=[tool], instructions=instructions,
                                       tool_choice='required')
            model_rounds += 1
            inputs += response['output']
            calls = [x for x in response['output'] if x['type'] == 'function_call']
            for call in calls:
                if call['name'] != 'preview_skill':
                    raise ValueError('unexpected_tool')
                request = json.loads(call['arguments'])
                key = json.dumps(request, sort_keys=True, separators=(',', ':'))
                if key in seen:
                    result = dict(planned=False, reason='identical_preview_already_returned',
                                  prior=seen[key])
                elif len(rows) >= 6:
                    result = dict(planned=False, reason='offline_probe_preview_budget_exhausted')
                else:
                    index = len(rows)
                    request_path = store.write_json(f'preview-{index}-request.json', request)
                    output_path = a.output / f'preview-{index}-result.json'
                    command = [str(root / '.venv-numerics/bin/python'),
                        str(root / 'scripts/preview_saved_scene.py'), '--site', str(a.site),
                        '--scene', str(a.scene), '--request', str(request_path),
                        '--output', str(output_path)]
                    started = time.monotonic()
                    with (a.output / f'preview-{index}-native.log').open('x') as log:
                        run = subprocess.run(command, env=dict(os.environ,
                            OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
                            PYTHONPATH=':'.join(str(v) for v in (
                                root/'src', root.parent/'DAgger/F1/src',
                                root.parent/'DAgger/shared/src', root/'third_party/RPent'))),
                            stdout=log, stderr=subprocess.STDOUT, timeout=90)
                    if run.returncode or not output_path.exists():
                        result = dict(planned=False, reason='native_preview_process_failed',
                                      exit_code=run.returncode)
                    else:
                        native = json.loads(output_path.read_text())
                        result = ({'planned':True, 'note':'native preview only; no execution'}
                                  if native['planned'] else native)
                    row = dict(turn=turn, request=request, result=result,
                               elapsed_s=time.monotonic()-started,
                               output=str(output_path), request_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest())
                    rows.append(row)
                    seen[key] = result
                    if result['planned'] and request['name'] == 'approach_grasp' and selected is None:
                        selected = row
                inputs.append(dict(type='function_call_output', call_id=call['call_id'],
                                   output=json.dumps(result, separators=(',', ':'))))
            if selected or len(rows) >= 6:
                break
    finally:
        planner.client.close()
    sources = [a.scene, a.geometry, a.failed_report, *a.images, Path(__file__),
               root/'scripts/preview_saved_scene.py']
    if a.prior_dialogue:sources.append(a.prior_dialogue)
    receipt = dict(scope='actual Astra/native saved-scene preview dialogue; no owner or physical execution',
        model_rounds=model_rounds, previews=rows, selected=selected,
        feasible_approach_found=selected is not None, hardware_outputs=0, robot_connections=0,
        source_sha256={str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in sources})
    store.write_json('summary.json', receipt)
    print(json.dumps(dict(model_rounds=model_rounds, native_previews=len(rows),
                         feasible_approach_found=selected is not None,
                         selected_parameters=None if selected is None else selected['request'])))


if __name__ == '__main__':
    main()
