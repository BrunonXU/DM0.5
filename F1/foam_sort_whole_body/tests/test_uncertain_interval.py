from pathlib import Path
import sys
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from isolate_uncertain_interval import isolate


def data():
 n=20
 return dict(time_ns=np.arange(n,dtype=np.int64)*10,state=np.ones((n,21)),action=np.ones((n,19)),
   state_valid=np.ones((n,21),bool),action_candidate_valid=np.ones((n,19),bool),
   base_timing_reviewed=np.ones(n,bool),future_signal_action_candidate_valid=np.ones(n,bool),
   joint_frame_candidate_valid=np.ones(n,bool),chunk_start_candidate_valid=np.r_[np.ones(17,bool),np.zeros(3,bool)],training_approved=np.array(False))


def test_crossing_chunks_and_source_values():
 p=data();out,bad=isolate(p,origin_ns=0,start_ns=80,stop_ns=100,horizon=4)
 assert np.flatnonzero(bad).tolist()==[8,9]
 assert not out['chunk_start_candidate_valid'][5:10].any()
 assert out['chunk_start_candidate_valid'][4] and out['chunk_start_candidate_valid'][10]
 for k in ['state','action','time_ns']:assert np.array_equal(out[k],p[k])
 assert p['action_candidate_valid'].all()


def test_never_reenables_prior_masks():
 p=data();p['chunk_start_candidate_valid'][0]=False;p['future_signal_action_candidate_valid'][15]=False
 out,_=isolate(p,origin_ns=0,start_ns=80,stop_ns=100,horizon=4)
 for k in p:
  if p[k].dtype==bool:assert not (out[k]&~p[k]).any()
 assert not out['chunk_start_candidate_valid'][12:16].any()


@pytest.mark.parametrize('start,stop',[(-1,10),(10,200),(30,20),(81,89)])
def test_invalid_interval_rejected(start,stop):
 with pytest.raises(ValueError):isolate(data(),origin_ns=0,start_ns=start,stop_ns=stop,horizon=4)
