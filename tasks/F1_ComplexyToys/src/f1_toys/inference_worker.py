"""One persistent execution thread for graph warmup and inference requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
from functools import partial


class InferenceWorker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='f1-inference')
        self.closed = False

    def submit(self, function, *args, **kwargs):
        if self.closed:
            raise RuntimeError('Inference worker is closed')
        context = contextvars.copy_context()
        return self.executor.submit(context.run, partial(function, *args, **kwargs))

    def run_sync(self, function, *args, **kwargs):
        return self.submit(function, *args, **kwargs).result()

    async def run(self, function, *args, **kwargs):
        return await asyncio.wrap_future(self.submit(function, *args, **kwargs))

    def close(self):
        if not self.closed:
            self.closed = True
            self.executor.shutdown(wait=True, cancel_futures=True)
