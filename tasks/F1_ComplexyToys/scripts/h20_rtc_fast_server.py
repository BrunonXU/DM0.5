"""H20 RTC runtime: established CUDA Graph plus exact-tested native math.

The frozen checkpoint, model core, original solver and preprocessing are retained.
Only the ArmModel math implementation is selected at policy construction.
"""
import inspect
import linecache
import os
from pathlib import Path
import textwrap

import torch
from f1_toys import rtc_optimization
from f1_toys.inference import Policy
from f1_toys.native import kinematics
from f1_toys.native.fast_kinematics import FastArmModel

original_init = Policy.__init__


def fast_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self.ik = kinematics.ContinuousIK(FastArmModel(
        Path(kinematics.__file__).with_name('ccs_m6_40.MvKDCfg')))


Policy.__init__ = fast_init


def compatible_compile(policy):
    # Construct and validate the opt-in prefix before mutating any model path.
    # RLT was trained on these eager functions. Keep them for its distinct
    # synchronized endpoint; this does not change the B RTC sampler paths.
    policy.rlt_reference_eager_prefix = policy.model.build_prefix_cache
    policy.rlt_reference_eager_suffix = policy.model.run_suffix
    from f1_toys.rtc_prefix_optimization import prepare_prefix_graph
    prefix_graph = prepare_prefix_graph(policy)
    cls = type(policy.model.llm)
    source = textwrap.dedent(inspect.getsource(cls.forward))
    before = 'isinstance(kv_cache, list | tuple)'
    after = 'isinstance(kv_cache, (list, tuple))'
    # Old frozen cores need the in-memory patch; new cores already use it.
    assert source.count(before) + source.count(after) == 1
    source = source.replace(before, after)
    filename = __file__+'.generated_forward'
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {}
    exec(compile(source, filename, 'exec'), cls.forward.__globals__, namespace)
    cls.forward = namespace['forward']
    os.environ['TORCH_COMPILE_DISABLE'] = '0'
    torch._dynamo.config.disable = False
    eager = policy.model.run_suffix
    compiled = torch.compile(eager, fullgraph=True, dynamic=False,
        options={'emulate_precision_casts': True, 'force_same_precision': True,
                 'triton.cudagraphs': True})

    def suffix(*args, **kwargs):
        if torch.is_inference_mode_enabled():
            return eager(*args, **kwargs)
        return compiled(*args, **kwargs)

    policy.model.run_suffix = suffix
    policy.rtc_optimization = 'compiled_suffix_precision_cudagraphs_v1_torch27_tuple_compat_native_math_v1'
    if prefix_graph is not None:
        prefix_graph.install()


rtc_optimization.compile_suffix = compatible_compile
original_predict = Policy.predict_rtc


def graph_predict(self, *args, **kwargs):
    torch.compiler.cudagraph_mark_step_begin()
    return original_predict(self, *args, **kwargs)


Policy.predict_rtc = graph_predict
from f1_toys.server import main
if __name__ == '__main__':
    main()
