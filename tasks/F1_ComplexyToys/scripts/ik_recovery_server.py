"""Opt-in RTC server: strict IK, bounded same-observation noise resampling.

No target projection, range change, model/sampler/VJP change or robot output.
The original successful response is returned untouched. Rejected responses stay
standard IK rejections; an opt-in client may inspect their valid diagnostics.
A budget bounds candidate selection, not GPU cancellation or network latency.
"""
from contextlib import contextmanager
import copy
import json
import math
from pathlib import Path
import secrets
import time


def tensor_leaves(value):
    import torch
    if isinstance(value, torch.Tensor):
        yield value
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from tensor_leaves(value[key])
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from tensor_leaves(item)
    elif hasattr(value, '__dict__'):
        yield from tensor_leaves(vars(value))
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError(f'Unsupported prefix tree {type(value)}')


def clone_tree(value):
    import torch
    if isinstance(value, torch.Tensor):
        if value.requires_grad or value.grad_fn is not None:
            raise ValueError('Prefix cache must be detached')
        return value.detach().clone()
    if isinstance(value, tuple):
        return tuple(clone_tree(v) for v in value)
    if isinstance(value, list):
        return [clone_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: clone_tree(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'Unsupported prefix cache {type(value)}')


@contextmanager
def request_prefix_cache(model):
    """Capture first build; clone only after failure, before the next graph step."""
    import torch
    original = model.build_prefix_cache
    state = dict(cache=None, original_cache=None, observation=None, builds=0, hits=0)

    def freeze():
        if state['original_cache'] is None:
            raise RuntimeError('RTC sampler did not build a prefix cache')
        state['cache'] = clone_tree(state['original_cache'])
        before = list(tensor_leaves(state['original_cache']))
        after = list(tensor_leaves(state['cache']))
        if any(a.numel() and a.data_ptr() == b.data_ptr() for a, b in zip(before, after)):
            raise RuntimeError('Prefix cache aliases original graph storage')
        state['versions'] = [t._version for t in after]
        state['original_cache'] = None

    def build(observation):
        leaves = list(tensor_leaves(observation))
        if state['builds'] == 0:
            result = original(observation)
            state['original_cache'] = result
            state['observation'] = [t.detach().clone() for t in leaves]
            state['builds'] += 1
            return result
        if state['cache'] is None:
            raise RuntimeError('Repeated prefix requested before failure freeze')
        expected = state['observation']
        if len(leaves) != len(expected) or any(a.shape != b.shape or a.dtype != b.dtype
                or a.device != b.device or not torch.equal(a, b) for a, b in zip(leaves, expected)):
            raise RuntimeError('Observation changed inside same-observation retry')
        cached = list(tensor_leaves(state['cache']))
        if [t._version for t in cached] != state['versions'] or any(
                t.requires_grad or t.grad_fn is not None for t in cached):
            raise RuntimeError('Prefix cache mutated or retained gradients')
        state['hits'] += 1
        return state['cache']

    state['freeze'] = freeze
    model.build_prefix_cache = build
    try:
        yield state
    finally:
        model.build_prefix_cache = original
        state['cache'] = state['original_cache'] = state['observation'] = None


def valid_suffix_steps(result):
    """Only the contiguous exact-success diagnostics before the rejection."""
    if result.get('success') is not False or result.get('reason') != 'ik_rejected' or result.get('actions') is not None:
        return 0
    diagnostics = result.get('diagnostics')
    if not isinstance(diagnostics, list) or not diagnostics or diagnostics[-1].get('success') is not False:
        return 0
    for diag in diagnostics[:-1]:
        joints = diag.get('joints')
        if diag.get('success') is not True or not isinstance(joints, (list, tuple)) or len(joints) != 7:
            return 0
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in joints):
            return 0
    return len(diagnostics) - 1


def recover_predict(policy, original, args, kwargs, *, max_attempts=4, budget_ms=400.,
                    clock=time.perf_counter, seed_factory=lambda: secrets.randbits(32),
                    cache_factory=request_prefix_cache, audit=None):
    """original is a bound predict_rtc. Return one entire real prediction tuple."""
    if not 1 <= max_attempts <= 4 or not math.isfinite(budget_ms) or budget_ms <= 0:
        raise ValueError('Recovery requires 1..4 total attempts and a positive finite budget')
    started = clock()
    # A per-attempt copy prevents a mutable preprocessing buffer from carrying
    # state/images/context changes into another candidate or back to the caller.
    frozen_args, frozen_kwargs = copy.deepcopy((args, kwargs))
    rtc = frozen_args[3] if len(frozen_args) > 3 else frozen_kwargs.get('rtc', {})
    correlation = {key: rtc.get(key) for key in ('bootstrap', 'base_plan_id', 'plan_origin_ns',
        'delay_steps', 'shift_steps', 'coverage_steps')}
    attempts = []
    selected = None
    selected_order = 0
    stop = 'attempt_limit'
    with cache_factory(policy.model) as cache:
        retry_seed_base = None
        for order in range(max_attempts):
            if order and (clock() - started) * 1000 >= budget_ms:
                stop = 'selection_budget_before_attempt'
                break
            call_args, call_kwargs = copy.deepcopy((frozen_args, frozen_kwargs))
            if order:
                call_kwargs['seed'] = (retry_seed_base + order - 1) % (2**63 - 1)
            began = clock()
            candidate = original(*call_args, **call_kwargs)
            elapsed = (clock() - started) * 1000
            result = candidate[0]
            eligible = order == 0 or elapsed < budget_ms
            attempts.append(dict(order=order, seed=call_kwargs.get('seed'),
                seed_source='caller_or_global_rng' if order == 0 else 'explicit_retry_seed',
                success=result.get('success'), valid_suffix_steps=valid_suffix_steps(result),
                call_ms=(clock() - began) * 1000, cumulative_ms=elapsed,
                eligible=eligible, reason=result.get('reason')))
            if order == 0 and result.get('success') is True:
                if audit:
                    audit(dict(event='ik_recovery', rtc_context=correlation, attempts=attempts, selected_order=0,
                        stop='original_success', total_ms=(clock()-started)*1000,
                        budget_ms=budget_ms, cache_builds=cache['builds'], cache_hits=cache['hits']))
                return candidate
            if order == 0:
                selected = copy.deepcopy(candidate)
                if result.get('reason') != 'ik_rejected' or result.get('success') is not False:
                    stop = 'not_ik_rejection'
                    break
            if order and result.get('success') is not True and not (
                    result.get('success') is False and result.get('reason') == 'ik_rejected'
                    and result.get('actions') is None):
                raise RuntimeError('Non-IK recovery candidate failure: ' + str(result.get('reason')))
            if not eligible:
                stop = 'selection_budget_after_attempt'
                break
            if result.get('success') is True:
                selected, selected_order = copy.deepcopy(candidate), order
                stop = 'strict_full_success'
                break
            if valid_suffix_steps(result) > valid_suffix_steps(selected[0]):
                selected, selected_order = copy.deepcopy(candidate), order
            if order == 0 and max_attempts > 1 and (clock()-started)*1000 < budget_ms:
                cache['freeze']()
                retry_seed_base = seed_factory()
        info = dict(schema='f1_strict_ik_recovery_v1', rtc_context=correlation, attempts=attempts,
            selected_order=selected_order, selected_seed=attempts[selected_order]['seed'],
            stop=stop, total_ms=(clock()-started)*1000, budget_ms=budget_ms,
            max_attempts=max_attempts, cache_builds=cache['builds'], cache_hits=cache['hits'],
            eef_postprocessed=False, selected_original_prediction=(selected_order == 0),
            resampled_prediction_selected=(selected_order > 0), ranges_changed=False,
            valid_suffix_steps=valid_suffix_steps(selected[0]))
    result, raw, data = selected
    result['ik_recovery'] = info
    # Preserve existing per-candidate phase times; expose accumulated work in
    # separate fields rather than misreport the selected candidate as total.
    result.setdefault('timing_ms', {})['recovery_total'] = info['total_ms']
    if audit:
        audit(dict(event='ik_recovery', **info))
    return result, raw, data


def install_recovery(policy_class, *, max_attempts=4, budget_ms=400., audit=None):
    original = policy_class.predict_rtc
    def predict(self, *args, **kwargs):
        return recover_predict(self, lambda *a, **k: original(self, *a, **k), args, kwargs,
            max_attempts=max_attempts, budget_ms=budget_ms, audit=audit)
    policy_class.predict_rtc = predict


def install_health(server_class, *, max_attempts=4, budget_ms=400., compat_path=None):
    """Add explicit wrapper capabilities to the existing aiohttp health body."""
    import hashlib
    identity = dict(wrapper='f1_strict_ik_recovery_v1', max_attempts=max_attempts,
        max_additional_attempts=max_attempts-1, budget_ms=budget_ms,
        budget_scope='per_request_candidate_selection_not_hard_cancellation',
        eef_postprocessed=False, ranges_changed=False,
        may_select_resampled_prediction=True, sampler='unchanged_full_vjp',
        partial_response='standard_ik_rejection_requires_opt_in_client',
        wrapper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    if compat_path is not None:
        identity['compat_sha256'] = hashlib.sha256(Path(compat_path).read_bytes()).hexdigest()
    original = server_class.health
    async def health(self, request):
        response = await original(self, request)
        body = json.loads(response.text)
        body['ik_recovery'] = dict(identity, enabled=bool(self.rtc))
        response.text = json.dumps(body, allow_nan=False)
        return response
    server_class.health = health


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--recovery-compat', type=Path, default=Path(__file__).with_name('h20_rtc_fast_server.py'))
    parser.add_argument('--recovery-max-attempts', type=int, default=4)
    parser.add_argument('--recovery-budget-ms', type=float, default=400.)
    parser.add_argument('--recovery-audit-log', type=Path, required=True)
    config, remaining = parser.parse_known_args()
    if not 1 <= config.recovery_max_attempts <= 4 or not math.isfinite(config.recovery_budget_ms) or config.recovery_budget_ms <= 0:
        parser.error('Recovery requires 1..4 attempts and positive finite budget')
    marker = 'from f1_toys.server import main'
    source = config.recovery_compat.read_text()
    if source.count(marker) != 1:
        raise ValueError('Unexpected compatibility wrapper entrypoint')
    exec(compile(source.split(marker)[0], str(config.recovery_compat), 'exec'),
         {'__file__': str(config.recovery_compat), '__name__': '_f1_recovery_compat'})
    from f1_toys.inference import Policy
    def audit(record):
        with config.recovery_audit_log.open('a') as stream:
            stream.write(json.dumps(record, allow_nan=False) + '\n')
    install_recovery(Policy, max_attempts=config.recovery_max_attempts,
                     budget_ms=config.recovery_budget_ms, audit=audit)
    sys.argv = [sys.argv[0], *remaining]
    from f1_toys.server import Server, main as server_main
    install_health(Server, max_attempts=config.recovery_max_attempts,
                   budget_ms=config.recovery_budget_ms, compat_path=config.recovery_compat)
    server_main()


if __name__ == '__main__':
    main()
