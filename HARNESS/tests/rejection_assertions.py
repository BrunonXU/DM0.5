def assert_rejected(result, reason):
    """A named admission failure remains a rejection, never a motion receipt."""
    assert result['lifecycle']=='rejected'
    assert result['no_motion'] is True
    assert reason in result['reason']
    assert result['outcome']!='success'
    return result
