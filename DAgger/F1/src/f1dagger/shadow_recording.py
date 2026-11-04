"""Shadow consumes live observations but never creates a recording dataset."""
from pathlib import Path
import tempfile

SHADOW_MODES = ('shadow', 'policy-shadow', 'sync-shadow')


class ShadowRecorder:
    """Fixed-size counters only: no queue, images, action arrays or journal files."""
    writer_error = None
    drops = 0

    def __init__(self):
        self.counts = dict(observations=0, policy_chunks=0, control=0, events=0)
        self.submitted = self.published = 0
        self.closed = False

    def _count(self, stream):
        if self.closed:
            return False
        self.counts[stream] += 1
        return True

    def record_event(self, *args, **kwargs):
        return self._count('events')

    def record_timestamped_observation(self, *args, **kwargs):
        return self._count('observations')

    def record_chunk(self, *args, **kwargs):
        return self._count('policy_chunks')

    def control(self, *, submitted=None, published=False, **kwargs):
        self.submitted += int(submitted is not None)
        self.published += int(bool(published))
        return self._count('control')

    def close(self, **kwargs):
        self.closed = True
        return True

    def summary(self):
        return dict(recording=False, counts=dict(self.counts), submitted=self.submitted,
                    published=self.published)


def workspace(runtime):
    """Temporary collision-model workspace, outside raw roots; removed at exit."""
    path = Path(runtime)
    path.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='.shadow-', dir=path)
