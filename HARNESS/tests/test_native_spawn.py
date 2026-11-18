"""Exercise the actual Python spawn loader used by the native control worker."""
from pathlib import Path
import subprocess
import sys


def test_spawn_import_does_not_reenter_native_argument_parser():
    entry=Path(__file__).resolve().parents[1]/'native/entry.py'
    script='''
import multiprocessing, sys
sys.modules['__main__'].__file__ = sys.argv[1]
sys.argv = ['f1dagger', 'harness-web']
child = multiprocessing.get_context('spawn').Process(target=print, args=('native-spawn-ready',))
child.start()
child.join(10)
if child.is_alive():
    child.terminate()
    child.join(2)
assert child.exitcode == 0, child.exitcode
'''
    result=subprocess.run([sys.executable,'-c',script,str(entry)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'native-spawn-ready' in result.stdout
