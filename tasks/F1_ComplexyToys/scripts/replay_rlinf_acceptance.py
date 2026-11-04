#!/usr/bin/env python3
"""Joint seeded model/PNG replay from saved F1 observations; no robot imports.

Both endpoints MUST advertise the temporary acceptance wrapper. Historical
physical state and RTC prefixes are fixed, never fed from new predictions.
The shared control lock excludes a simultaneous live run. Failures are saved.
"""
import argparse
import csv
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
import uuid
from urllib.parse import urlparse
from urllib.request import urlopen


def sha(data):
    return hashlib.sha256(data).hexdigest()


def percentiles(values):
    import numpy as np
    if not values:
        return {}
    return dict(zip(('p50', 'p95', 'p99', 'max'), np.percentile(values, [50, 95, 99, 100]).tolist()))


def checked_health(endpoint, model, variant, seed):
    parsed = urlparse(endpoint)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path not in ('', '/'):
        raise ValueError('Require an explicit F1 loopback tunnel endpoint')
    with urlopen(endpoint.rstrip('/') + '/health', timeout=5) as response:
        health = json.load(response)
    for key in ('model_id', 'model_version'):
        if health.get(key) != model[key]:
            raise ValueError('Model identity mismatch: ' + key)
    if not health.get('ready') or health.get('protocol') != 'f1_eef_rtc_v2' or health.get('denoise_steps') != 5:
        raise ValueError('RTC health contract mismatch')
    identity = health.get('offline_acceptance', {})
    expected = dict(seed=seed, variant=variant, schema='f1_rlinf_offline_acceptance_v1', fixed_seed=True, production=False)
    if identity != expected:
        raise ValueError('Endpoint is not the explicitly seeded temporary acceptance server: ' + repr(identity))
    return health


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalog', type=Path, required=True)
    p.add_argument('--model', default='v1_dagger_rightonly_1000')
    p.add_argument('--client', type=Path, required=True)
    p.add_argument('--client-manifest-sha256', required=True)
    p.add_argument('--baseline-endpoint', required=True)
    p.add_argument('--candidate-endpoint', required=True)
    p.add_argument('--inputs', type=Path, required=True)
    p.add_argument('--source-run', type=Path, required=True)
    p.add_argument('--execution-location', choices=('f1', 'h20-localhost'), default='f1')
    p.add_argument('--pixels', type=Path, help='Explicit matching processor copy for H20 localhost replay')
    p.add_argument('--local-replay-lock', type=Path, help='Own acceptance lock, required only on H20; never the robot lock')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--rounds', type=int, default=1)
    p.add_argument('--warmup-pairs', type=int, default=2)
    p.add_argument('--seed', type=int, default=2026102301)
    p.add_argument('--timeout', type=float, default=5.)
    p.add_argument('--interval', type=float, default=.05)
    p.add_argument('--include-graph-only', action='store_true')
    args = p.parse_args()
    if args.rounds < 1 or args.warmup_pairs < 0 or args.timeout <= 0 or args.interval < 0:
        p.error('Invalid repetition or timeout')
    if args.baseline_endpoint.rstrip('/') == args.candidate_endpoint.rstrip('/'):
        p.error('Baseline and candidate must be distinct isolated servers')
    if args.execution_location == 'h20-localhost' and (not args.local_replay_lock or not args.pixels):
        p.error('H20 localhost requires explicit own replay lock and processor copy')
    if args.execution_location == 'f1' and args.local_replay_lock:
        p.error('F1 must use its catalog shared control lock')
    args.output.mkdir(parents=True, exist_ok=False)
    status = dict(status='RUNNING', started_wall_ns=time.time_ns(), robot_commands=0,
                  live_camera_subscription=False, robot_modules_imported=False,
                  requests=0, control_lock_released=False)
    save_status = lambda: (args.output/'status.json').write_text(json.dumps(status, indent=2)+'\n')
    save_status()
    catalog = json.loads(args.catalog.read_text()); model = catalog['models'][args.model]
    if model['contract'] != 'v1_dagger_rightonly_eef':
        raise ValueError('This acceptance matrix is right-only; legacy Full is ineligible')
    manifest_path = args.client/'F1_RELEASE_MANIFEST.json'
    if sha(manifest_path.read_bytes()) != args.client_manifest_sha256:
        raise ValueError('Candidate client manifest mismatch')
    manifest = json.loads(manifest_path.read_text())
    for rel, expected in manifest['files'].items():
        if sha((args.client/rel).read_bytes()) != expected:
            raise ValueError('Candidate client file changed: ' + rel)
    sys.path[:0] = [str(args.client/'src'), catalog['robot']['pythonpath']]
    import numpy as np
    from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
    from f1dagger.rtc_pixels import PixelEncoder
    from f1dagger.websocket_policy import WebSocketPolicy, encode_request
    import f1dagger.rtc_pixels as pixel_module
    import f1dagger.websocket_policy as websocket_module
    encoders, transports, records, pairs = {}, {}, [], []
    lock = (open(args.local_replay_lock, 'a+') if args.execution_location == 'h20-localhost'
            else open(catalog['robot']['control_lock'], 'r+'))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        health = {
            'baseline': checked_health(args.baseline_endpoint, model, 'baseline', args.seed),
            'candidate': checked_health(args.candidate_endpoint, model, 'candidate', args.seed)}
        assert health['baseline']['input_preprocessing'] == health['candidate']['input_preprocessing']
        graph = health['candidate'].get('rtc_prefix_graph', {})
        assert graph.get('enabled') is True and graph.get('graph_ready') is True
        assert graph.get('failed') is False
        assert graph.get('mode') == 'eager_prefix_graph_no_autocast_cache_v1'
        assert graph.get('prefix_autocast_cache') is False and graph.get('single_inference_worker') is True
        initial_graph_replays = health['candidate']['rtc_prefix_graph_diagnostics']['replay_count']
        assert not health['baseline'].get('rtc_prefix_graph', {}).get('enabled', False)
        workers = catalog['contracts'][model['contract']]['config']['policy']['rtc_pixel_workers']
        pixels = args.pixels or model['pixels']
        encoders['baseline'] = PixelEncoder(pixels, health['baseline']['input_preprocessing'], workers=workers, masked_left_transport=False)
        encoders['candidate'] = PixelEncoder(pixels, health['candidate']['input_preprocessing'], workers=workers, masked_left_transport=True)
        marker = encoders['candidate'].transport_identity
        assert marker and health['candidate'].get('image_transport') == marker
        assert encoders['candidate'].contract['active_cameras'] == ['head', 'right_wrist']
        for variant, endpoint in (('baseline', args.baseline_endpoint), ('candidate', args.candidate_endpoint)):
            transports[variant] = WebSocketPolicy({'endpoint':endpoint.rstrip('/'), 'timeout_s':args.timeout}, '', lambda _:None, held_getter=lambda:None)
        inputs = json.loads(args.inputs.read_text())
        if len(inputs) != 24:
            raise ValueError('Expected the fixed 24-observation acceptance set')
        rows = {row['observation_id']:row for row in map(json.loads, (args.source_run/'payload/frames.jsonl').open())}
        samples = []
        for entry in inputs:
            row = rows[entry['observation_id']]
            assert row['state16'] == entry['state16']
            frames = []
            for f in row['frames']:
                assert f['path'] == f['camera']+'.pack'
                with (args.source_run/'payload'/f['path']).open('rb') as stream:
                    stream.seek(f['payload_offset']); data = stream.read(f['payload_bytes'])
                assert sha(data) == f['payload_sha256']
                frames.append(TimestampedFrame(f['camera'], f['frame_id'], DeviceTimestamp(**f['timestamp']),
                    f['host_received_monotonic_ns'], data, f['media_type'], f['width'], f['height']))
            samples.append((entry, row, tuple(frames)))
        session = 'rlinf-joint-acceptance-'+uuid.uuid4().hex
        provenance = dict(catalog_release=catalog['release'], model=args.model, health=health,
            session_id=session, seed=args.seed, client=str(args.client), client_manifest_sha256=args.client_manifest_sha256,
            source_run=str(args.source_run), input_sha256=sha(args.inputs.read_bytes()),
            source_sha256={str(x):sha(Path(x).read_bytes()) for x in (pixel_module.__file__,websocket_module.__file__)},
            workers=workers, unique_observations=len(samples), rounds=args.rounds,
            warmup_pairs=args.warmup_pairs, marker=marker,
            execution_location=args.execution_location, actual_f1_network=args.execution_location=='f1',
            no_robot_modules_or_publishers=True, source_images_unchanged=True)
        (args.output/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
        for pair_index in range(len(samples)*args.rounds):
            entry, row, frames = samples[pair_index % len(samples)]
            request_id = 100000 + pair_index
            rtc = dict(entry['rtc']); rtc['base_plan_id'] = 0 if rtc['bootstrap'] else request_id-1
            metadata = dict(contract='f1_upper_16d_v1', protocol='f1_eef_rtc_v2', session_id=session,
                request_id=request_id, observation_id=row['observation_id'], policy_epoch=0,
                observation_monotonic_ns=row['capture_started_monotonic_ns'], state16=entry['state16'],
                last_submitted16=entry['last_submitted16'], state_timestamps_ns=row['state_timestamps_ns'],
                prediction_horizon=50, execute_steps=20, chunk_steps=20, frequency_hz=30., shadow_only=True,
                model_id=model['model_id'], model_version=model['model_version'], prepared_pixels=encoders['baseline'].identity, rtc=rtc)
            if row.get('state_valid_mask') is not None:
                metadata.update(state_valid_mask=row['state_valid_mask'], absent_state_semantics=row['absent_state_semantics'])
            variants = ['baseline','candidate']
            if args.include_graph_only:
                variants.insert(1,'graph_only')
            if pair_index % 2:
                variants.reverse()
            responses, pixel_hashes, pair_rows = {}, {}, {}
            for variant in variants:
                masked = variant == 'candidate'; endpoint_variant = 'baseline' if variant == 'baseline' else 'candidate'
                record = dict(pair=pair_index, variant=variant, request_id=request_id,
                    observation_id=row['observation_id'], bootstrap=rtc['bootstrap'], seed=args.seed,
                    success=False, response_verified=False, hard_prefix_exact=None)
                started = time.perf_counter(); response = {}
                try:
                    encoder = encoders['candidate' if masked else 'baseline']
                    prepared = encoder.encode(frames)
                    encoded = time.perf_counter()
                    pixel_hashes[variant] = {f.camera:sha(f.payload) for f in prepared}
                    for raw_frame, encoded_frame in zip(frames, prepared):
                        assert (raw_frame.camera,raw_frame.frame_id,raw_frame.timestamp,raw_frame.host_received_monotonic_ns) == (encoded_frame.camera,encoded_frame.frame_id,encoded_frame.timestamp,encoded_frame.host_received_monotonic_ns)
                        assert encoded_frame.media_type == 'image/png' and (encoded_frame.width,encoded_frame.height)==(224,224)
                    request_meta = dict(metadata)
                    if masked:
                        request_meta['image_transport'] = marker
                        assert pixel_hashes[variant]['left_wrist'] == marker['png_sha256']
                    payload = encode_request(request_meta, prepared)
                    packed = time.perf_counter()
                    record.update(payload_bytes=len(payload),image_bytes=sum(len(f.payload) for f in prepared),
                        pixel_preprocess_png_ms=(encoded-started)*1000,pack_ms=(packed-started)*1000,
                        payload_sha256=sha(payload),pixel_sha256=pixel_hashes[variant],actual_png=True,
                        inactive_placeholder_cameras=['left_wrist'] if masked else [])
                    response = transports[endpoint_variant]._exchange(payload)
                    finished = time.perf_counter()
                    for key in ('contract','protocol','session_id','request_id','observation_id','policy_epoch','observation_monotonic_ns','model_id','model_version'):
                        assert response.get(key) == request_meta[key], 'Response identity mismatch: '+key
                    proof = response.get('offline_acceptance',{})
                    for key,value in health[endpoint_variant]['offline_acceptance'].items():
                        assert proof.get(key) == value, 'Seed/variant receipt mismatch: '+key
                    assert len(proof.get('raw_sha256','')) == 64 and proof.get('raw_dtype') == 'float32'
                    assert proof.get('raw_shape') == [50,32]
                    if endpoint_variant == 'candidate':
                        assert proof.get('prefix_graph', {}).get('graph_ready') is True
                        assert proof['prefix_graph'].get('failed') is False
                        assert proof['prefix_graph_diagnostics']['replay_count'] > initial_graph_replays
                    assert response.get('shadow_only') is True
                    if masked:
                        assert response.get('image_transport') == marker
                    else:
                        assert response.get('image_transport') in (None, {})
                    for key in ('bootstrap','delay_steps','shift_steps','coverage_steps','plan_origin_ns','base_plan_id'):
                        assert response['rtc_context'][key] == rtc[key]
                    record.update(response_verified=True,success=response.get('success') is True,
                        server=response.get('timing_ms'),network_rtt_ms=response.get('client_network_rtt_ms'),
                        client_total_ms=(finished-started)*1000,transport_stages=response.get('client_transport_stages'),
                        raw_sha256=proof['raw_sha256'],image_transport=response.get('image_transport'))
                    if record['success']:
                        actions=np.asarray(response['actions']); absolute=np.asarray(response['absolute_eef10'])
                        assert actions.shape == (rtc['coverage_steps'],16) and np.isfinite(actions).all()
                        assert absolute.shape == (50,10) and np.isfinite(absolute).all()
                        record['hard_prefix_exact'] = rtc['bootstrap'] or np.array_equal(actions[:rtc['delay_steps']], np.asarray(rtc['committed_actions16']))
                        assert record['hard_prefix_exact'], 'Committed hard prefix changed'
                except Exception as exc:
                    record.update(response_verified=False, validation_error=f'{type(exc).__name__}: {exc}',
                        error=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc(), client_total_ms=(time.perf_counter()-started)*1000)
                record['response'] = response
                if response.get('error') and not record.get('error'):
                    record['error'] = response['error']
                records.append(record); pair_rows[variant]=record; responses[variant]=response
                with (args.output/'requests.jsonl').open('a') as stream:
                    stream.write(json.dumps(record,allow_nan=False)+'\n')
                status['requests'] += 1; save_status()
                time.sleep(args.interval)
            pair = dict(pair=pair_index,observation_id=row['observation_id'],bootstrap=rtc['bootstrap'],comparisons={})
            for variant in variants:
                if variant == 'baseline':continue
                a,b=responses['baseline'],responses[variant]
                ra,rb=pair_rows['baseline'],pair_rows[variant]
                valid=(ra['response_verified'] and rb['response_verified']
                    and not ra.get('validation_error') and not rb.get('validation_error')
                    and ra['hard_prefix_exact'] is not False and rb['hard_prefix_exact'] is not False)
                item=dict(response_verified=valid,active_pixels_exact=all(k in pixel_hashes.get('baseline',{}) and k in pixel_hashes.get(variant,{}) and pixel_hashes['baseline'][k]==pixel_hashes[variant][k] for k in ('head','right_wrist')),
                    raw_exact=valid and ra['raw_sha256']==rb['raw_sha256'],success_equal=valid and a.get('success')==b.get('success'),
                    actions_exact=valid and a.get('actions')==b.get('actions'),absolute_eef_exact=valid and a.get('absolute_eef10')==b.get('absolute_eef10'))
                item['pass']=all(item.values());pair['comparisons'][variant]=item
            pair['pass']=all(x['pass'] for x in pair['comparisons'].values());pairs.append(pair)
            with (args.output/'pairs.jsonl').open('a') as stream:stream.write(json.dumps(pair)+'\n')
            print(json.dumps(dict(completed_pairs=len(pairs),passed=pair['pass'],requests=len(records))),flush=True)
        metrics={}
        for variant in sorted({r['variant'] for r in records}):
            selected=[r for r in records if r['variant']==variant and r['pair']>=args.warmup_pairs and r['response_verified']]
            metrics[variant]={key:percentiles([r[key] for r in selected if r.get(key) is not None]) for key in ('payload_bytes','image_bytes','pack_ms','pixel_preprocess_png_ms','client_total_ms','network_rtt_ms')}
            for key in ('preprocess','model_h2d_d2h','ik','policy','decode','handler','worker_queue','worker_total','log_and_replay'):
                metrics[variant]['server_'+key+'_ms']=percentiles([r['server'][key] for r in selected if key in (r.get('server') or {})])
        final_candidate_health=checked_health(args.candidate_endpoint,model,'candidate',args.seed)
        assert final_candidate_health['rtc_prefix_graph']['failed'] is False
        assert final_candidate_health['rtc_prefix_graph']['graph_ready'] is True
        graph_replays=final_candidate_health['rtc_prefix_graph_diagnostics']['replay_count']-initial_graph_replays
        expected_graph_replays=sum(r['variant']!='baseline' and r['response_verified'] for r in records)
        assert graph_replays>=expected_graph_replays
        (args.output/'final-candidate-health.json').write_text(json.dumps(final_candidate_health,indent=2)+'\n')
        summary=dict(status='PASS' if all(x['pass'] for x in pairs) else 'FAIL', pairs=len(pairs),passed_pairs=sum(x['pass'] for x in pairs),
            requests=len(records),protocol_failures=sum(not r['response_verified'] for r in records),
            pair_phase_counts=dict(bootstrap=sum(x['bootstrap'] for x in pairs),guided=sum(not x['bootstrap'] for x in pairs)),
            request_phase_counts=dict(bootstrap=sum(x['bootstrap'] for x in records),guided=sum(not x['bootstrap'] for x in records)),
            unique_observation_phase_counts=dict(bootstrap=sum(x['rtc']['bootstrap'] for x in inputs),guided=sum(not x['rtc']['bootstrap'] for x in inputs)),
            ik_rejections=sum(r['response_verified'] and not r['success'] for r in records),
            hard_prefix_failures=sum(r['hard_prefix_exact'] is False for r in records),metrics=metrics,
            robot_commands=0,model_inference_requests=len(records),warmup_pairs=args.warmup_pairs,
            execution_location=args.execution_location,actual_f1_network=args.execution_location=='f1',
            actual_prefix_graph_replays=graph_replays, verified_candidate_requests=expected_graph_replays,
            limitations=['Saved observations: no live camera/control/recording load.',
                'H20 localhost results do not measure F1 ARM packing or the real F1-to-H20 link.' if args.execution_location=='h20-localhost' else 'F1 history replay includes the real link but not live control load.',
                'Same hardware family on distinct GPUs; raw hash directly checks equality for this matrix.',
                'H20 localhost RTT includes loopback WebSocket/OS scheduling, server work and client JSON decode; no SSH or F1 wireless link.' if args.execution_location=='h20-localhost' else 'F1 RTT includes SSH, transport, server work and scheduling; it is not pure network.',
                'IK rejection retained; paired equal rejection is not task success.',
                'Offline same-seed proposals do not prove physical task performance.'])
        (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        with (args.output/'metrics.csv').open('w') as stream:
            writer=csv.DictWriter(stream,fieldnames=['variant','metric','p50','p95','p99','max']);writer.writeheader()
            for variant,values in metrics.items():
                for metric,stats in values.items():writer.writerow(dict(variant=variant,metric=metric,**stats))
        status['status']=summary['status'];print(json.dumps(summary),flush=True)
    except BaseException as exc:
        status.update(status='ERROR',error=f'{type(exc).__name__}: {exc}',traceback=traceback.format_exc())
        raise
    finally:
        for transport in transports.values():transport.close()
        for encoder in encoders.values():encoder.close()
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
        status.update(finished_wall_ns=time.time_ns(),control_lock_released=True);save_status()
    return 0 if status['status']=='PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
