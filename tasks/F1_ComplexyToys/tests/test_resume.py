import pytest
from f1_toys.resume import legacy_progress


@pytest.mark.parametrize('step,accum,batches,offset,epoch', [
    (750,1,114,66,6), (114,1,114,114,0), (115,1,114,1,1),
    (0,1,114,0,0), (750,4,455,270,6),
])
def test_legacy_epoch_and_boundary(step,accum,batches,offset,epoch):
    assert legacy_progress(step,accum,batches,{'_num_yielded':offset}) == (epoch,offset)


def test_inconsistent_cursor_fails_closed():
    with pytest.raises(ValueError,match='geometry'):
        legacy_progress(750,1,100,{'_num_yielded':66})
