"""F1 must remain usable without installing any G2 package."""
import os
from pathlib import Path
import subprocess
import sys


def test_f1_imports_without_g2(tmp_path):
    root=Path(__file__).resolve().parents[2]
    env=dict(os.environ,PYTHONPATH=os.pathsep.join(str(root/p) for p in ('F1/src','shared/src')))
    code='''
import sys
class NoG2:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('g2dagger','g2teleop','g2_sort'):
            raise AssertionError('F1 imported a G2 implementation: '+fullname)
sys.meta_path.insert(0,NoG2())
from f1dagger.policy import F1PolicyEngine
from f1dagger.recording import F1Recorder
from f1dagger.teleop import F1ArmTarget
from f1dagger.network import IntentServer
assert F1Recorder.schema_version == 3
'''
    subprocess.run([sys.executable,'-c',code],cwd=tmp_path,env=env,check=True,capture_output=True,text=True)
