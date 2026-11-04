"""Human-paced input must not leave a collective receive pending during idle.

No Ray cluster, gradients, network IO, or robot publisher is started here.
"""
import asyncio
from collections import deque
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip('torch')
pytest.importorskip('ray')
pytest.importorskip('omegaconf')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, os.environ.get('F1_RLT_CORE', str(Path(__file__).resolve().parents[3] / 'RLinf')))
from f1_toys import rlt_async_workers as workers


class ReplyingChannel:
    """Model the pinned API: nowait replies even when no data is available."""
    def __init__(self, values=()):
        self.values = deque(values)
        self.queries = 0

    def get(self, *args, **kwargs):
        raise AssertionError('Do not post an indefinite collective receive')

    def get_nowait(self):
        self.queries += 1
        if not self.values:
            raise asyncio.QueueEmpty
        return self.values.popleft()


def test_idle_over_collective_timeout_then_data_is_consumed_once(monkeypatch):
    channel = ReplyingChannel()
    worker = SimpleNamespace(stopping=False)
    elapsed = 0
    real_sleep = asyncio.sleep

    async def advance(seconds):
        nonlocal elapsed
        assert seconds > 0  # No idle busy spin.
        elapsed += seconds
        if elapsed >= 10801:
            channel.values.append('new-real-cache')
        await real_sleep(0)

    monkeypatch.setattr(workers.asyncio, 'sleep', advance)
    result = asyncio.run(workers._receive_when_available(worker, channel))
    assert elapsed > 10800
    assert result == 'new-real-cache' and not channel.values


def test_stop_during_idle_does_not_consume_a_later_item(monkeypatch):
    channel = ReplyingChannel()
    worker = SimpleNamespace(stopping=False)

    async def stop(_seconds):
        worker.stopping = True
        channel.values.append('unconsumed')

    monkeypatch.setattr(workers.asyncio, 'sleep', stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workers._receive_when_available(worker, channel))
    assert channel.queries == 1 and list(channel.values) == ['unconsumed']


def test_real_transport_error_is_not_treated_as_an_empty_queue():
    failure = RuntimeError('transport disconnected')
    class BrokenChannel:
        def get_nowait(self):
            raise failure
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(workers._receive_when_available(SimpleNamespace(stopping=False), BrokenChannel()))
    assert caught.value is failure


def test_actor_accepts_late_cache_without_fake_training_or_budget_credit(monkeypatch):
    channel = ReplyingChannel()
    ingested = []
    worker = SimpleNamespace(stopping=False, recv_task=None)
    def ingest(path):
        ingested.append(path)
        worker.stopping = True
    worker._ingest = ingest
    sleeps = []
    async def arrival(seconds):
        sleeps.append(seconds)
        channel.values.append('validated-cache')
    monkeypatch.setattr(workers.asyncio, 'sleep', arrival)
    asyncio.run(workers.F1ActorWorker.recv_rollout_trajectories(worker, channel))
    assert sleeps and ingested == ['validated-cache']
    assert channel.queries == 2


def test_rollout_accepts_late_receipt_and_preserves_interrupted_review(tmp_path, monkeypatch):
    run = tmp_path / 'episode'
    (run / 'local_segments').mkdir(parents=True)
    (run / 'local_segments/placement-1.json').write_text(json.dumps({
        'episode_id': 'episode', 'local_result': {'status': 'interrupted'}}))
    channel = ReplyingChannel()
    worker = SimpleNamespace(stopping=False, task=None, recipe={}, cfg=SimpleNamespace(f1={}))
    outputs = []
    class Output:
        def put(self, value, *, async_op):
            assert async_op
            outputs.append(value)
            worker.stopping = True
            class Work:
                async def async_wait(self):
                    pass
            return Work()
    async def arrival(_seconds):
        channel.values.append(str(run))
    monkeypatch.setattr(workers.asyncio, 'sleep', arrival)
    asyncio.run(workers.F1RolloutWorker.generate(worker, channel, Output(), None))
    assert outputs == [{'pending_review': True}]
    assert channel.queries == 2


def test_actor_cancel_while_waiting_exits_without_ingesting():
    async def check():
        channel = ReplyingChannel()
        worker = SimpleNamespace(stopping=False, recv_task=None,
            _ingest=lambda path: pytest.fail('No data should be consumed'))
        task = asyncio.create_task(workers.F1ActorWorker.recv_rollout_trajectories(worker, channel))
        await asyncio.sleep(0)
        assert channel.queries == 1 and not task.done()
        task.cancel()
        await task
        assert task.done()
    asyncio.run(check())
