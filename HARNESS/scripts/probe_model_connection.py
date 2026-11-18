"""Uncredentialed, unbilled official API route probe; emit only redacted timings."""
import argparse
import json
from pathlib import Path
import time

import httpx
from f1_harness.model_transport import ModelTransportTrace
from f1_harness.planner import transport_failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('new_evidence_path_required')
    rows = []
    with httpx.Client(timeout=httpx.Timeout(8, connect=3),
                      limits=httpx.Limits(keepalive_expiry=120)) as client:
        for attempt in range(3):
            trace, started = ModelTransportTrace(), time.monotonic()
            try:
                response = client.get('https://api.openai.com/v1/models', extensions={'trace': trace})
                result = dict(http_status=response.status_code)
            except httpx.HTTPError as exc:
                code, phase = transport_failure(exc)
                result = dict(code=code, phase=phase, error_type=type(exc).__name__)
            rows.append(dict(attempt=attempt+1, elapsed_s=time.monotonic()-started,
                             **trace.summary(), **result))
    value = dict(credential_used=False, generation_requested=False, rows=rows,
                 limitation='401 proves HTTP reachability only, not account/model access or future stability')
    args.output.write_text(json.dumps(value, indent=2)+'\n')
    print(json.dumps(value))


if __name__ == '__main__':
    main()
