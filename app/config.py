import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./certificates.db"
    storage_dir: Path = Path("./generated")
    # Upper bound on recipients in a single request. Keeps request bodies and
    # single-job runtimes bounded; larger batches should be split by the client.
    max_recipients_per_job: int = 5000
    # Number of jobs processed concurrently by the in-process worker pool.
    worker_threads: int = 2


def load_settings() -> Settings:
    """Build settings from environment variables, falling back to defaults."""
    defaults = Settings()
    return Settings(
        database_url=os.getenv("DATABASE_URL", defaults.database_url),
        storage_dir=Path(os.getenv("STORAGE_DIR", str(defaults.storage_dir))),
        max_recipients_per_job=int(
            os.getenv("MAX_RECIPIENTS_PER_JOB", defaults.max_recipients_per_job)
        ),
        worker_threads=int(os.getenv("WORKER_THREADS", defaults.worker_threads)),
    )
