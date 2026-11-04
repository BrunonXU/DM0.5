#!/usr/bin/env python3
"""Temporary seeded acceptance adapter; never used by production launchers.

Select the frozen source using PYTHONPATH, then run this with --wrapper pointing
at that release's original H20 launcher. All remaining arguments pass through.
The policy produces proposals only; this program has no robot dependency.
"""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wrapper', type=Path, required=True)
    parser.add_argument('--acceptance-variant', choices=('baseline', 'candidate'), required=True)
    parser.add_argument('--acceptance-seed', type=int, default=2026102301)
    args, server_args = parser.parse_known_args()
    if server_args[:1] == ['--']:
        server_args = server_args[1:]
    if not args.wrapper.is_file():
        parser.error('Frozen H20 wrapper must exist')
    if not 0 <= args.acceptance_seed < 2**63:
        parser.error('Acceptance seed must be a nonnegative int64')
    import numpy as np
    from aiohttp import web
    from f1_toys.inference import Policy
    from f1_toys.server import Server

    identity = dict(seed=args.acceptance_seed, variant=args.acceptance_variant,
                    schema='f1_rlinf_offline_acceptance_v1', fixed_seed=True,
                    production=False)
    original_predict = Policy.predict_rtc
    original_health = Server.health

    def seeded_predict(self, *predict_args, **kwargs):
        kwargs['seed'] = args.acceptance_seed
        decoded, raw, data = original_predict(self, *predict_args, **kwargs)
        raw_bytes = np.ascontiguousarray(raw, dtype=np.float32).tobytes(order='C')
        decoded['offline_acceptance'] = dict(identity, raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                                             raw_shape=list(np.shape(raw)), raw_dtype='float32')
        graph = getattr(self, 'rtc_prefix_graph', None)
        if graph is not None:
            decoded['offline_acceptance']['prefix_graph'] = dict(graph.status)
            decoded['offline_acceptance']['prefix_graph_diagnostics'] = dict(graph.diagnostics)
        return decoded, raw, data

    async def acceptance_health(self, request):
        response = await original_health(self, request)
        payload = json.loads(response.body)
        payload['offline_acceptance'] = identity
        return web.json_response(payload, status=response.status)

    Policy.predict_rtc = seeded_predict
    Server.health = acceptance_health
    # The H20 wrapper captures our seeded method before applying its own graph/
    # math wrapper. Its normal construction and warmup remain unchanged.
    sys.argv = [str(args.wrapper), *server_args]
    runpy.run_path(str(args.wrapper), run_name='__main__')


if __name__ == '__main__':
    main()
