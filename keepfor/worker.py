# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from keepfor.app import app
from keepfor.consumer.processor import process_queue_batch

try:
    from workers import WorkerEntrypoint, asgi

    class KeepForMeWorker(WorkerEntrypoint):
        # NOTE: the runtime dispatches fetch(request, env, ctx) and
        # queue(batch, env, ctx). Signatures must accept all three: a
        # narrower queue(self, batch) took down every delivery with
        # "takes 2 positional arguments but 4 were given" (prod, 2026-10-03).
        async def fetch(self, request, env=None, ctx=None):
            return await asgi.fetch(app, request, env or self.env)

        async def queue(self, batch, env=None, ctx=None):
            await process_queue_batch(batch, env or self.env)

except ImportError:
    # Local runtime / testing fallback
    pass


async def on_fetch(request, env):
    from workers import asgi

    return await asgi.fetch(app, request, env)


async def on_queue(batch, env):
    await process_queue_batch(batch, env)
