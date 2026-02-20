"""
Celery application instance.
Import from here in all task files: from app.core.celery_app import celery_app
"""
from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "telecom_platform",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.sms_tasks",
        "app.tasks.notification_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)