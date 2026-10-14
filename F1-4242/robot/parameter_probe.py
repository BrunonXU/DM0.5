"""One bounded parameter read in an isolated RMW; no motor command imports."""
import json
import sys
from .runtime_contract import read_motor_parameters


def main():
    try:
        request=json.load(sys.stdin)
        result=read_motor_parameters(request['expected'],request['timeout_s'])
        print(json.dumps({'ok':True,'result':result},allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'error':f'{type(exc).__name__}: {exc}'}))
        return 2


if __name__=='__main__':raise SystemExit(main())
