"""Explicit non-recording sink for emergency manual exit after disk failure.

True denotes that the disabled path was handled, never persistence. No files,
training receipts or completion manifests can be produced by this object.
"""
class UnrecordedEgress:
    writer_error = None
    drops = 0
    recording = False
    def record_event(self, *a, **kw): return True
    def record_timestamped_observation(self, *a, **kw): return True
    def control(self, *a, **kw): return True
    def trigger(self, *a, **kw): return True
    def submit(self, *a, **kw): return True
    def record_chunk(self, *a, **kw):
        raise RuntimeError('Policy cannot produce chunks during unrecorded manual exit')
