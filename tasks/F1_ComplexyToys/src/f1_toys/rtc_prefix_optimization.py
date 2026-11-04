"""Opt-in prefix CUDA graph, using the exact-tested eager prefix operations.

The two constant constructions and prefix-only disabled AMP cache match the
2026-10-23 r4 candidate. Sampling, suffix compilation and IK are untouched.
"""
from __future__ import annotations

import inspect
import linecache
import textwrap
import threading
import traceback
from contextlib import nullcontext
from types import MethodType

import torch

from .input_optimization import environment_switch, require_right_only
from .rtc_prefix_graph import EagerPrefixGraph

MODE = 'eager_prefix_graph_no_autocast_cache_v1'
_MISSING = object()


def _restore_attribute(instance, name, saved):
    if saved is _MISSING:
        delattr(instance, name)
    else:
        setattr(instance, name, saved)


def _replace_method(bound_method, before, after, tag):
    original = bound_method.__func__
    source = textwrap.dedent(inspect.getsource(original))
    if source.count(before) != 1:
        raise ValueError(f'{tag}: expected exactly one validated frozen-source constant')
    source = source.replace(before, after)
    filename = __file__ + '.' + tag
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {}
    exec(compile(source, filename, 'exec'), original.__globals__, namespace)
    return MethodType(namespace[original.__name__], bound_method.__self__)


class PrefixGraphOptimization:
    def __init__(self, policy):
        require_right_only(policy)
        self.policy, self.model = policy, policy.model
        if (not str(policy.device).startswith('cuda') or self.model.training
                or self.model.action_horizon != 50 or self.model.action_dim != 32
                or list(self.model._config.action_loss_mask) != [True] * 10 + [False] * 22):
            raise ValueError('Prefix graph requires frozen CUDA inference with the validated H50 action mask')
        if hasattr(policy, 'rtc_prefix_graph'):
            raise ValueError('Prefix graph is already configured')
        self.original_prefix = self.model.build_prefix_cache
        self.original_prefix_slot = vars(self.model).get('build_prefix_cache', _MISSING)
        self.fixed_embed = _replace_method(
            self.model.embed_prefix,
            'ar_mask = torch.tensor(ar_mask, device=tokens.device)',
            "assert not any(ar_mask), 'Prefix graph requires all-False AR mask'\n"
            '    ar_mask = torch.zeros(len(ar_mask), dtype=torch.bool, device=tokens.device)',
            'embed_prefix_device_constant')
        self.attentions = []
        for index, layer in enumerate(self.model.llm.layers):
            attention = layer.attn
            fixed = _replace_method(
                attention.forward,
                'torch.tensor(big_neg, dtype=logits.dtype, device=logits.device)',
                'torch.full((), big_neg, dtype=logits.dtype, device=logits.device)',
                f'attention_device_constant_{index}')
            self.attentions.append((attention, fixed))
        if not self.attentions:
            raise ValueError('Prefix graph found no validated Gemma attention layers')
        self.selected = EagerPrefixGraph(self._patched_prefix)
        self.closed = False
        self.failed = False
        self.last_traceback = None
        self.worker_thread = None
        self.replays = 0
        self.image_reuse = None
        self.reuse_this_call = False
        if environment_switch('F1_RTC_PREFIX_MASKED_CACHE'):
            from .inactive_image_reuse import InactiveImageReuse
            self.image_reuse = InactiveImageReuse(self.model)

    def _patched_prefix(self, observation):
        previous_embed = vars(self.model).get('embed_prefix', _MISSING)
        previous_forwards = [(attention, vars(attention).get('forward', _MISSING))
                             for attention, _ in self.attentions]
        self.model.embed_prefix = self.fixed_embed
        try:
            for attention, fixed in self.attentions:
                attention.forward = fixed
            scope = (self.image_reuse.apply(observation)
                     if self.reuse_this_call else nullcontext())
            with scope:
                return self.original_prefix(observation)
        finally:
            _restore_attribute(self.model, 'embed_prefix', previous_embed)
            for attention, forward in previous_forwards:
                _restore_attribute(attention, 'forward', forward)

    def __call__(self, observation):
        if self.closed or self.failed:
            raise RuntimeError('Prefix graph is unavailable; restart the validated service')
        thread = threading.get_ident()
        if self.worker_thread is not None and self.worker_thread != thread:
            raise RuntimeError('Prefix graph must run on its single warmed inference worker')
        self.worker_thread = thread
        try:
            # Match the r4 candidate: casts created by warmup cannot become
            # short-lived graph inputs from the surrounding autocast cache.
            with torch.autocast('cuda', enabled=torch.is_autocast_enabled('cuda'),
                                dtype=torch.get_autocast_dtype('cuda'), cache_enabled=False):
                self.reuse_this_call = self.image_reuse is not None and not torch.is_inference_mode_enabled()
                if self.reuse_this_call and self.image_reuse.prepare(observation):
                    self.selected = EagerPrefixGraph(self._patched_prefix)
                output = self.selected(observation)
            if not torch.is_inference_mode_enabled():
                self.replays += 1
            return output
        except Exception:
            self.failed = True
            self.last_traceback = traceback.format_exc()
            raise  # A failed graph never silently falls back to another path.

    @property
    def status(self):
        return dict(enabled=not self.closed, mode=MODE, input_version='right_only_v1',
                    inactive_image_reuse=self.image_reuse is not None,
                    graph_ready=bool(self.selected.graph is not None and self.replays
                                     and not self.failed and not self.closed),
                    failed=self.failed,
                    prefix_autocast_cache=False, single_inference_worker=True)

    @property
    def diagnostics(self):
        return dict(replay_count=self.replays,
                    inactive_image_cache_hits=self.image_reuse.hits if self.image_reuse else 0,
                    inactive_image_cache_resets=self.image_reuse.resets if self.image_reuse else 0)

    def install(self):
        if self.closed or self.model.build_prefix_cache != self.original_prefix:
            raise ValueError('Prefix path changed while preparing graph installation')
        try:
            self.model.build_prefix_cache = self
            self.policy.rtc_prefix_graph = self
        except Exception:
            _restore_attribute(self.model, 'build_prefix_cache', self.original_prefix_slot)
            if getattr(self.policy, 'rtc_prefix_graph', None) is self:
                delattr(self.policy, 'rtc_prefix_graph')
            raise
        return self

    def close(self):
        if self.closed:
            return
        if self.model.build_prefix_cache is self:
            _restore_attribute(self.model, 'build_prefix_cache', self.original_prefix_slot)
        if getattr(self.policy, 'rtc_prefix_graph', None) is self:
            delattr(self.policy, 'rtc_prefix_graph')
        self.closed = True


def prepare_prefix_graph(policy):
    """Validate and construct before mutating model or policy attributes."""
    return PrefixGraphOptimization(policy) if environment_switch('F1_RTC_PREFIX_GRAPH') else None


def prefix_graph_status(policy):
    handle = getattr(policy, 'rtc_prefix_graph', None)
    return handle.status if handle is not None else dict(enabled=False, mode='eager', graph_ready=False)
