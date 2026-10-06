import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

logger = logging.getLogger(__name__)

JobProcessor = Callable[[str], None]


class JobRunner(Protocol):
    def submit(self, job_id: str) -> None: ...

    def shutdown(self) -> None: ...


class ThreadPoolJobRunner:
    """Runs jobs in a bounded pool of background threads inside the API process.

    The pool size caps how many jobs generate at once, so a burst of large
    requests queues up instead of exhausting the server. Job state lives in the
    database, so anything lost on a crash is picked up again at startup.
    """

    def __init__(self, process: JobProcessor, max_workers: int):
        self._process = process
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="certgen")

    def submit(self, job_id: str) -> None:
        self._pool.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        try:
            self._process(job_id)
        except Exception:  # never let a job kill the worker thread silently
            logger.exception("Unhandled error while processing job %s", job_id)

    def shutdown(self) -> None:
        # Drop queued jobs (they stay `pending` in the DB and resume on next
        # startup) but let the jobs already running finish.
        self._pool.shutdown(wait=True, cancel_futures=True)


class InlineJobRunner:
    """Processes the job synchronously in the caller. Used in tests for determinism."""

    def __init__(self, process: JobProcessor):
        self._process = process

    def submit(self, job_id: str) -> None:
        self._process(job_id)

    def shutdown(self) -> None:
        pass
