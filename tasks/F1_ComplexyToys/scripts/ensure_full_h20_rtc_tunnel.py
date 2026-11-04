"""Compatibility alias. Tunnel ownership and identity checks live in the runtime."""
import os
import sys

if __name__ == '__main__':
    if len(sys.argv)!=2:
        raise SystemExit('Usage: ensure_full_h20_rtc_tunnel.py STEP')
    entry=os.environ.get('F1_INFERENCE_ENTRY',
        '/home/user/Bruno/DAgger-runtime/inference-current/inference_runtime.py')
    os.execvp('python3',['python3',entry,'--model','full'+sys.argv[1],'--check'])
