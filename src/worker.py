from src.app import app
from src.consumer.processor import process_queue_batch

try:
    from workers import WorkerEntrypoint, asgi

    class KeepForMeWorker(WorkerEntrypoint):
        async def fetch(self, request):
            return await asgi.fetch(app, request, self.env)

        async def queue(self, batch):
            await process_queue_batch(batch, self.env)

except ImportError:
    # Local runtime / testing fallback
    pass

async def on_fetch(request, env):
    from workers import asgi
    return await asgi.fetch(app, request, env)

async def on_queue(batch, env):
    await process_queue_batch(batch, env)
