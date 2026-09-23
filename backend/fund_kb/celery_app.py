"""Celery transports job IDs; authoritative state/attempts live in the database.

Start: celery -A fund_kb.celery_app:app worker --loglevel=INFO
Recovery can run in the API lifespan or a separate Celery beat process.
"""
from __future__ import annotations

import threading

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown, worker_shutdown

from .settings import get_settings

_runtime = None
_runtime_lock = threading.RLock()


def _worker_runtime(settings):
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            from .db import build_engine, make_session_factory
            from .jobs import JobDispatcher
            engine = build_engine(settings.database_url)
            index = None
            if settings.retrieval_mode == "hybrid":
                from .retrieval import VectorIndex
                index = VectorIndex(settings)
            dispatcher = JobDispatcher(settings, make_session_factory(engine), index)
            _runtime = (dispatcher, index, engine)
            from .model_warmup import start_default_warmup
            start_default_warmup(settings, index)
        return _runtime[0]


def create_celery_app(settings=None):
    settings = settings or get_settings()
    broker = getattr(settings, "celery_broker_url", None) or getattr(settings, "rabbitmq_url", None)
    if not broker:
        raise RuntimeError("CELERY_BROKER_REQUIRED")
    application = Celery("fund_kb", broker=broker)
    application.conf.update(
        accept_content=["json"], task_serializer="json", result_serializer="json",
        task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1, worker_cancel_long_running_tasks_on_connection_loss=True,
        broker_connection_retry_on_startup=True, broker_connection_timeout=5,
        broker_transport_options={"confirm_publish": True},
        task_publish_retry=True, task_publish_retry_policy={"max_retries": 2, "interval_start": 0,
                                                         "interval_step": .5, "interval_max": 1},
        timezone="UTC", enable_utc=True,
        beat_schedule={"recover-durable-jobs": {"task": "fund_kb.recover_jobs", "schedule": 10.0}},
    )

    @application.task(name="fund_kb.run_job", ignore_result=True)
    def run_job(job_id):
        return _worker_runtime(settings).run(job_id)

    @application.task(name="fund_kb.recover_jobs", ignore_result=True)
    def recover_jobs():
        return _worker_runtime(settings)._recover_once()

    return application


@worker_process_init.connect
def prepare_worker_runtime(**_):
    settings = get_settings()
    if (settings.retrieval_mode == "hybrid" and settings.embedding_mode == "transformers"
            and settings.reranker_mode == "local" and settings.retrieval_warmup_mode == "auto"):
        _worker_runtime(settings)


@worker_process_shutdown.connect
@worker_shutdown.connect
def close_worker_runtime(**_):
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            dispatcher, index, engine = _runtime
            dispatcher.close()
            if index is not None:
                index.close()
            engine.dispose()
            _runtime = None


app = create_celery_app()
