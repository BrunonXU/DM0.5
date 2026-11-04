"""Isolated eager-prefix CUDA capture experiment, with reversible constants fix.

The two host-created CUDA constants are suspected capture blockers; the old
receipt did not retain the original traceback, so this is not a proven fix.
``constants`` checks numerical parity first. ``graph`` then wraps the established
EagerPrefixGraph. ``graph_no_autocast_cache`` tests ownership of AMP weight casts
created during pre-capture warmup, by disabling that cache only in the prefix.
Only the selected model instance is affected, only while its prefix runs; the
action suffix and frozen core sources are unchanged.
"""
from __future__ import annotations

import inspect
import linecache
from pathlib import Path
import textwrap
import traceback
from types import MethodType

import torch

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
        raise ValueError(f"{tag}: expected exactly one frozen-source constant")
    source = source.replace(before, after)
    filename = __file__ + '.' + tag
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {}
    exec(compile(source, filename, 'exec'), original.__globals__, namespace)
    return MethodType(namespace[original.__name__], bound_method.__self__)


class PrefixCandidate:
    def __init__(self, policy, mode, error_path=None):
        if mode not in ('constants', 'graph', 'graph_no_autocast_cache'):
            raise ValueError("Prefix mode must be constants, graph or graph_no_autocast_cache")
        self.model, self.mode = policy.model, mode
        self.error_path = Path(error_path) if error_path else None
        self.original_prefix = self.model.build_prefix_cache
        self.original_prefix_slot = vars(self.model).get('build_prefix_cache', _MISSING)
        self.original_embed = self.model.embed_prefix
        self.closed, self.last_traceback = False, None
        self.debug_sync = False
        self.last_stage = 'initialized'
        self.fixed_embed = _replace_method(
            self.original_embed,
            'ar_mask = torch.tensor(ar_mask, device=tokens.device)',
            "assert not any(ar_mask), 'Prefix constant candidate requires all-False AR mask'\n"
            '    ar_mask = torch.zeros(len(ar_mask), dtype=torch.bool, device=tokens.device)',
            'embed_prefix_device_constant')
        self.attentions = []
        for index, layer in enumerate(self.model.llm.layers):
            attention = layer.attn
            original = attention.forward
            fixed = _replace_method(
                original,
                'torch.tensor(big_neg, dtype=logits.dtype, device=logits.device)',
                'torch.full((), big_neg, dtype=logits.dtype, device=logits.device)',
                f'attention_device_constant_{index}')
            self.attentions.append((attention, original, fixed))
        if not self.attentions:
            raise ValueError('Prefix candidate found no frozen Gemma attention layers')
        if mode in ('graph', 'graph_no_autocast_cache'):
            from f1_toys.rtc_prefix_graph import EagerPrefixGraph
            self.selected = EagerPrefixGraph(self._patched_prefix)
        else:
            self.selected = self._patched_prefix

    def _patched_prefix(self, observation):
        # Attribute patches are instance-local and restored before the suffix,
        # even when CUDA capture raises. No class or module is rewritten.
        previous_embed = vars(self.model).get('embed_prefix', _MISSING)
        previous_forwards = [(attention, vars(attention).get('forward', _MISSING))
                             for attention, _, _ in self.attentions]
        self.model.embed_prefix = self.fixed_embed
        try:
            for attention, _, fixed in self.attentions:
                attention.forward = fixed
            return self.original_prefix(observation)
        finally:
            _restore_attribute(self.model, 'embed_prefix', previous_embed)
            for attention, forward in previous_forwards:
                _restore_attribute(attention, 'forward', forward)

    def __call__(self, observation):
        if self.closed:
            raise RuntimeError('Prefix candidate is closed')
        try:
            self.last_stage = 'before_prefix'
            if self.debug_sync:
                torch.cuda.synchronize()
            self.last_stage = 'prefix'
            if self.mode == 'graph_no_autocast_cache':
                # Warmup and capture must not borrow transient BF16 weight
                # tensors from the surrounding Policy autocast cache. Keep
                # autocast enabled/dtype unchanged, and restore all AMP state
                # before the compiled suffix runs.
                with torch.autocast('cuda',
                                    enabled=torch.is_autocast_enabled('cuda'),
                                    dtype=torch.get_autocast_dtype('cuda'),
                                    cache_enabled=False):
                    output = self.selected(observation)
            else:
                output = self.selected(observation)
            self.last_stage = 'after_prefix'
            if self.debug_sync:
                torch.cuda.synchronize()
            self.last_stage = 'completed'
            return output
        except Exception:
            self.last_traceback = traceback.format_exc()
            if self.error_path:
                self.error_path.parent.mkdir(parents=True, exist_ok=True)
                self.error_path.write_text('stage: '+self.last_stage+'\n'+self.last_traceback)
            raise

    def close(self):
        if self.closed:
            return
        if self.model.build_prefix_cache is self:
            _restore_attribute(self.model, 'build_prefix_cache', self.original_prefix_slot)
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def install_prefix_candidate(policy, mode='constants', *, error_path=None):
    """Return a restorable handle; graph failures retain their full traceback.

    Example: with install_prefix_candidate(policy, 'constants'): predict_rtc(...)
    For repeated alternating comparisons, keep the handle and select it as
    policy.model.build_prefix_cache; select handle.original_prefix for baseline.
    For fault localization set handle.debug_sync=True during warmup/correctness,
    then False before uninstrumented timing. It defaults to False.
    """
    if isinstance(policy.model.build_prefix_cache, PrefixCandidate):
        raise ValueError('Close the existing prefix candidate before installing another')
    candidate = PrefixCandidate(policy, mode, error_path)
    policy.model.build_prefix_cache = candidate
    return candidate
