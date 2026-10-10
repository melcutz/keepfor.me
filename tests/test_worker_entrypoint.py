# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Contract test for the Cloudflare worker entrypoint signatures.

The runtime dispatches queue(batch, env, ctx) positionally. A narrower
queue(self, batch) crashed EVERY delivery in prod with
"TypeError: ... takes 2 positional arguments but 4 were given"
(2026-10-03): 998 messages ingested, ~808 acked, zero items processed.

The `workers` package only exists on the Cloudflare runtime, so this is
skipped locally and in CI -- it runs nowhere today, and exists to pin the
contract in an obvious place next to the code it protects.
"""

import pytest


def test_worker_entrypoint_accepts_env_and_ctx():
    workers = pytest.importorskip("workers", reason="Cloudflare Workers runtime only")
    import inspect

    assert hasattr(workers, "WorkerEntrypoint")
    from keepfor.worker import KeepForMeWorker

    queue_params = list(inspect.signature(KeepForMeWorker.queue).parameters)
    assert queue_params == ["self", "batch", "env", "ctx"]
    fetch_params = list(inspect.signature(KeepForMeWorker.fetch).parameters)
    assert fetch_params == ["self", "request", "env", "ctx"]
