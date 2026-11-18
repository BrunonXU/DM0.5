import sys
from pathlib import Path
import numpy as np
import pytest
from scipy.spatial.transform import Rotation
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from support_plane_diagnostic import plane_clearance


def test_translation_reports_real_downward_excursion():
    a=np.eye(4);b=a.copy();b[2,3]=-.001
    result=plane_clearance(a,b,[[0,0,0]],[0,0,1],[0,0,0])
    assert result['arc_min_m']==pytest.approx(-.001) and result['downward_excursion_m']==pytest.approx(.001)


def test_interior_dip_is_found_even_when_endpoints_are_above_plane():
    a=np.eye(4);a[:3,:3]=Rotation.from_euler('y',-60,degrees=True).as_matrix();a[2,3]=.8
    b=a.copy();b[:3,:3]=Rotation.from_euler('y',60,degrees=True).as_matrix()
    result=plane_clearance(a,b,[[0,0,-1]],[0,0,1],[0,0,0])
    assert result['initial_min_m']>.29 and result['final_min_m']>.29
    assert result['arc_min_m']==pytest.approx(-.2) and result['arc_min_fraction']==pytest.approx(.5)


def test_stationary_and_rising_contact_are_not_penetration():
    a=np.eye(4);b=a.copy();b[2,3]=.02
    for end in (a,b):
        result=plane_clearance(a,end,[[0,0,0],[0,0,.06]],[0,0,1],[0,0,0])
        assert result['arc_min_m']==0 and result['downward_excursion_m']==0


def test_analytic_minimum_matches_dense_independent_arc_samples():
    rng=np.random.default_rng(352)
    for _ in range(30):
        a=np.eye(4);a[:3,:3]=Rotation.random(random_state=rng).as_matrix();a[:3,3]=rng.normal(size=3)
        b=np.eye(4);b[:3,:3]=Rotation.random(random_state=rng).as_matrix();b[:3,3]=rng.normal(size=3)
        corners=rng.normal(size=(8,3));n=rng.normal(size=3);n/=np.linalg.norm(n);p=rng.normal(size=3)
        result=plane_clearance(a,b,corners,n,p)
        t=np.linspace(0,1,20001);omega=Rotation.from_matrix(a[:3,:3].T@b[:3,:3]).as_rotvec()
        rotations=np.einsum('ij,njk->nik',a[:3,:3],Rotation.from_rotvec(t[:,None]*omega).as_matrix())
        distances=np.einsum('nki,ji,k->nj',rotations,corners,n)+(a[:3,3]+t[:,None]*(b[:3,3]-a[:3,3])-p)@n[:,None]
        dense=distances.min()
        assert result['arc_min_m']<=dense+1e-12
        assert dense-result['arc_min_m']<2e-7
