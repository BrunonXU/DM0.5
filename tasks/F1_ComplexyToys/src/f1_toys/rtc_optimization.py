"""Explicit, versioned inference optimization; frozen checkpoint/core unchanged."""
import os
import torch


def compile_suffix(policy):
    """Compile only action-expert forward/backward, preserving complete RTC VJP.

    Load the frozen core with TORCH_COMPILE_DISABLE=1 first, so unrelated core
    decorators stay disabled. Bootstrap and guided paths must both warm before
    health becomes ready. Cache paths belong to the immutable service release.
    """
    if getattr(policy, 'rtc_optimization', 'eager') != 'eager':
        raise ValueError('RTC optimization already configured')
    os.environ['TORCH_COMPILE_DISABLE'] = '0'
    torch._dynamo.config.disable = False
    policy.model.run_suffix = torch.compile(policy.model.run_suffix,
        fullgraph=True, dynamic=False,
        options={'emulate_precision_casts': True, 'force_same_precision': True})
    policy.rtc_optimization = 'compiled_suffix_precision_v1'
