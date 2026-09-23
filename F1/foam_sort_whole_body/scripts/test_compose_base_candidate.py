import unittest
import numpy as np
from compose_base_candidate import compose_arrays


class CompositionTests(unittest.TestCase):
    def inputs(self):
        n = 120
        a = np.ones((n, 19), dtype=bool)
        a[:11] = False
        a[100:] = False
        a[65, 3] = False  # non-tail technical failure must survive
        chunks = np.array([i+50 <= n and a[i:i+50].all() for i in range(n)])
        parent = dict(time_ns=np.arange(n, dtype=np.int64)*33333333,
                      state=np.zeros((n,21)), action=np.ones((n,19)),
                      state_valid=np.ones((n,21), dtype=bool), action_candidate_valid=a,
                      observation_candidate_valid=np.ones(n,dtype=bool),
                      chunk_start_candidate_valid=chunks,
                      training_approved=np.array(False))
        base = dict(time_ns=parent['time_ns'].copy(), base_candidate_v=np.full((n,3), .1),
                    base_candidate_present=np.ones(n,dtype=bool), base_timing_reviewed=np.ones(n,dtype=bool))
        return parent, base

    def test_parent_restrictions_and_nonbase_values_preserved(self):
        parent, base = self.inputs()
        result = compose_arrays(parent,base,50)
        self.assertTrue(np.array_equal(result['action'][:,:16], parent['action'][:,:16]))
        self.assertTrue(np.array_equal(result['state'], parent['state']))
        self.assertFalse(result['action_candidate_valid'][:11].any())
        self.assertFalse(result['action_candidate_valid'][100:].any())
        self.assertFalse(result['action_candidate_valid'][65,3])
        self.assertFalse(bool(result['training_approved']))
        self.assertTrue(np.all(parent['action'] == 1))

    def test_unknown_invalidates_every_chunk_crossing_it(self):
        parent, base = self.inputs()
        base['base_candidate_present'][60]=False
        base['base_timing_reviewed'][60]=False
        base['base_candidate_v'][60]=np.nan
        result=compose_arrays(parent,base,50)
        self.assertFalse(result['chunk_start_candidate_valid'][11:61].any())
        for i in range(120):
            expected=bool(parent['chunk_start_candidate_valid'][i] and
                          i+50<=120 and all(result['action_candidate_valid'][j].all() for j in range(i,i+50)))
            self.assertEqual(bool(result['chunk_start_candidate_valid'][i]),expected)

    def test_unreviewed_is_visible_but_not_supervised(self):
        parent,base=self.inputs()
        base['base_timing_reviewed'][:]=False
        result=compose_arrays(parent,base,50)
        self.assertTrue(np.isfinite(result['action'][:,16:]).all())
        self.assertFalse(result['action_candidate_valid'][:,16:].any())
        self.assertFalse(result['chunk_start_candidate_valid'].any())

    def test_reject_time_mismatch(self):
        parent,base=self.inputs()
        base['time_ns'][20]+=1
        with self.assertRaisesRegex(ValueError,'times differ'): compose_arrays(parent,base,50)

    def test_reject_unknown_filled_as_zero(self):
        parent,base=self.inputs()
        base['base_candidate_present'][20]=False
        base['base_timing_reviewed'][20]=False
        base['base_candidate_v'][20]=0
        with self.assertRaisesRegex(ValueError,'unknown base must be NaN'): compose_arrays(parent,base,50)


if __name__=='__main__': unittest.main()
