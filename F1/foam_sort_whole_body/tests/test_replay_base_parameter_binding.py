from pathlib import Path
from types import SimpleNamespace as NS
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay.ros_backend import RosBackend
class BaseParameterBindingTests(unittest.TestCase):
 def make(self,values,expected=None):
  expected=expected or {'max_acceleration.linear':.13}
  b=object.__new__(RosBackend);b.profile={'base_controller_expected_parameters':expected};b._opened=True;b._context=NS(ok=lambda:True);b.calls=[]
  f=NS(done=lambda:True,result=lambda:NS(values=values))
  c=NS(service_is_ready=lambda:True,call_async=lambda req:(b.calls.append(('request',req.names)) or f))
  b._node=NS(create_client=lambda typ,name:(b.calls.append(('client',name)) or c),destroy_client=lambda cl:b.calls.append(('destroy',True)))
  b._get_parameters_type=NS(Request=lambda:NS(names=[]));return b
 def test_reads_only_expected_controller_parameters(self):
  b=self.make([NS(type=3,double_value=.13)])
  self.assertEqual(b.verify_base_parameters()['parameters'],{'max_acceleration.linear':.13})
  self.assertEqual(b.calls[0],('client','/controller_node/get_parameters'))
  self.assertEqual(b.calls[-1],('destroy',True))
 def test_mismatch_missing_nonfinite_rejected_and_client_closed(self):
  for values in [[],[NS(type=0)],[NS(type=3,double_value=.5)],[NS(type=3,double_value=float('nan'))]]:
   b=self.make(values)
   with self.subTest(values=values),self.assertRaises(RuntimeError):b.verify_base_parameters()
   self.assertEqual(b.calls[-1],('destroy',True))
 def test_existing_profiles_make_no_new_client(self):
  b=object.__new__(RosBackend);b.profile={};self.assertIsNone(b.verify_base_parameters())
if __name__=='__main__':unittest.main()
