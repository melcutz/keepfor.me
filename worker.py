from src import worker as _worker

KeepForMeWorker = getattr(_worker, "KeepForMeWorker", None)
on_fetch = _worker.on_fetch
on_queue = _worker.on_queue

__all__ = ["KeepForMeWorker", "on_fetch", "on_queue"]
