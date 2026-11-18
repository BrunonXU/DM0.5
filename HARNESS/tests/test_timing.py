import json

from f1_harness.timing import inspect_timing


def test_audit_preserves_duplicates_reversals_and_retention_limits(tmp_path):
    records=[]
    for index, stamp in enumerate((100,100,90,120)):
        records.append(dict(kind='image', topic='/camera/head/color/image_raw',
                            source_ns=stamp, received_ns=1000+index*10,
                            blob_sha256=str(index)))
    records.append(dict(kind='image', topic='/camera/head/depth/image_raw',
                        source_ns=0, received_ns=1000, blob_sha256='depth'))
    path=tmp_path/'capture.json'
    path.write_text(json.dumps(dict(records=records, image_interval_s=.5,
                                   source_clock_verified=True)))
    raw=path.read_bytes()
    report=inspect_timing(tmp_path)
    rgb, depth=report['streams']
    assert rgb['repeated_source_intervals']==1
    assert rgb['reversed_source_intervals']==1
    assert rgb['repeated_stamp_with_changed_content']==1
    assert depth['missing_or_zero_source_stamp']==1
    assert report['retention']['image_interval_s']==.5
    assert not report['source_clock_verified'] and not report['geometry_admitted']
    assert path.read_bytes()==raw
