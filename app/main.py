import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI

from app import services
from app.api import router
from app.config import Settings, load_settings
from app.database import Base, make_engine, make_session_factory
from app.renderer import render_certificate
from app.storage import CertificateStorage
from app.worker import JobProcessor, JobRunner, ThreadPoolJobRunner

logger = logging.getLogger(__name__)

RunnerFactory = Callable[[JobProcessor, Settings], JobRunner]


def _default_runner(process: JobProcessor, settings: Settings) -> JobRunner:
    return ThreadPoolJobRunner(process, max_workers=settings.worker_threads)


def create_app(
    settings: Settings | None = None,
    runner_factory: RunnerFactory = _default_runner,
    renderer: services.Renderer = render_certificate,
) -> FastAPI:
    """Application factory. Tests inject their own settings, runner and renderer."""
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    storage = CertificateStorage(settings.storage_dir)
    process = partial(services.process_job, session_factory, storage, render=renderer)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        app.state.runner = runner_factory(process, settings)

        # Resume jobs that were queued or mid-flight when the process last stopped.
        for job_id in services.unfinished_job_ids(session_factory):
            logger.info("Resuming unfinished job %s", job_id)
            app.state.runner.submit(job_id)

        yield

        app.state.runner.shutdown()
        engine.dispose()

    app = FastAPI(
        title="Bulk Certificate Generator",
        version="1.0.0",
        description="Submit recipients in bulk, track generation progress, download PDF certificates.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.storage = storage
    app.include_router(router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
