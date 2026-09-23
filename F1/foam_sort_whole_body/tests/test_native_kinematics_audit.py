"""Numerical audit harness tests; these never load F1 libraries or ROS."""
import ctypes
import importlib.util
from pathlib import Path
import unittest

import numpy as np

PATH = Path(__file__).resolve().parents[1] / 'scripts/kinematics_sdk_worker.py'
spec = importlib.util.spec_from_file_location('native_kinematics_worker_test', PATH)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class KinematicsHarnessTests(unittest.TestCase):
    def test_loaded_pnva_positive_negative_order(self):
        pnva = np.zeros((2,7,4)); pnva[:,:,2:] = [180,450]
        effective = worker.apply_controller_limits(pnva)
        self.assertTrue((effective[:,:,0] > effective[:,:,1]).all())
        self.assertEqual(effective[1,1,0],120)
        self.assertEqual(effective[1,1,1],-94)
        np.testing.assert_array_equal(effective[:,:,2:],pnva[:,:,2:])
        self.assertEqual(pnva[1,1,1],0)

    def test_installed_header_abi_offsets(self):
        self.assertEqual(ctypes.sizeof(worker.SolveParams), 992)
        self.assertEqual(worker.SolveParams.reference.offset, 128)
        self.assertEqual(worker.SolveParams.nsp_type.offset, 184)
        self.assertEqual(worker.SolveParams.joints.offset, 272)
        self.assertEqual(worker.SolveParams.result_num.offset, 840)

    def test_fixed_anchor_rotation_and_translation_roundtrip(self):
        anchor = np.eye(4)
        anchor[:3,:3] = [[0,-1,0],[1,0,0],[0,0,1]]
        anchor[:3,3] = [.2,-.5,.8]
        target = np.eye(4)
        target[:3,:3] = [[1,0,0],[0,-1,0],[0,0,-1]]
        target[:3,3] = [-.3,.1,.4]
        before_a, before_t = anchor.copy(), target.copy()
        recovered = worker.relative_roundtrip(anchor,target)
        np.testing.assert_allclose(recovered,target,atol=1e-14)
        np.testing.assert_array_equal(anchor,before_a)
        np.testing.assert_array_equal(target,before_t)

    def test_ik_converts_copy_to_mm_without_mutating_target(self):
        class Call:
            def __call__(self, arm, ptr):
                params = ctypes.cast(ptr,ctypes.POINTER(worker.SolveParams)).contents
                self.target = np.ctypeslib.as_array(params.target).copy()
                params.joints[:] = [1,2,3,4,5,6,7]
                params.result_num = 1
                return 1
        class Library:
            FX_Robot_Kine_IK = Call()
        sdk = worker.NativeKinematics.__new__(worker.NativeKinematics)
        sdk.lib = Library()
        target = np.eye(4); target[:3,3] = [.1,.2,.3]
        before = target.copy()
        result = sdk.ik(0,target,np.arange(7))
        np.testing.assert_array_equal(target,before)
        np.testing.assert_allclose(sdk.lib.FX_Robot_Kine_IK.target[:3,3],[100,200,300])
        self.assertEqual(result['joints'],[1,2,3,4,5,6,7])

    def test_nonfinite_or_wrong_arm_rejected_before_native_call(self):
        sdk = worker.NativeKinematics.__new__(worker.NativeKinematics)
        for arm,q in [(2,np.zeros(7)),(0,np.zeros(6)),(1,np.full(7,np.nan))]:
            with self.assertRaises(ValueError): sdk.fk(arm,q)
        with self.assertRaises(ValueError): sdk.ik(0,np.full((4,4),np.nan),np.zeros(7))


if __name__ == '__main__':
    unittest.main()
