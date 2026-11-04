from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_admission import record_admission, admission_summary


def test_rejected_new_review_is_visible_despite_old_buffered_data(tmp_path):
    record_admission(tmp_path, 'episode', 'actor', 'buffered', source_revision='old', split='train')
    record_admission(tmp_path, 'episode', 'rollout', 'rejected', source_revision='new', error='bad publication')
    result = admission_summary(tmp_path)
    assert result['rejected'] == 1 and result['buffered'] == 0
    assert result['episodes'][0]['error'] == 'bad publication'
    record_admission(tmp_path, 'episode', 'rollout', 'prepared', source_revision='new')
    assert admission_summary(tmp_path)['episodes'][0]['status'] == 'prepared'
    record_admission(tmp_path, 'episode', 'actor', 'buffered', source_revision='new', split='validation')
    assert admission_summary(tmp_path)['buffered'] == 1
    assert admission_summary(tmp_path)['episodes'][0]['split'] == 'validation'
