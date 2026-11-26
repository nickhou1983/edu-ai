import logging
from threading import Event
from uuid import UUID

from sqlalchemy import and_, or_, select

from edu_app.agent_client import Agent
from edu_app.db import Job, now_ms
from edu_app.jobs import WORKER_TIMEOUT_SECONDS, run_one
from edu_app.runtime import Runtime

logger = logging.getLogger(__name__)
DISPATCH_RECOVERY_MS = 20 * 60_000


def _client(runtime: Runtime):
    from azure.servicebus import ServiceBusClient

    return ServiceBusClient(runtime.settings.service_bus_namespace, runtime.credential, retry_total=1)


def _dispatch(runtime: Runtime, sender) -> bool:
    from azure.servicebus import ServiceBusMessage

    timestamp = now_ms()
    with runtime.db.transaction() as session:
        job = session.scalar(
            select(Job)
            .where(
                or_(
                    and_(
                        Job.status == "queued",
                        Job.available_at <= timestamp,
                        or_(
                            Job.dispatched_at.is_(None), Job.dispatched_at < timestamp - DISPATCH_RECOVERY_MS
                        ),
                    ),
                    and_(
                        Job.status == "running",
                        Job.lease_until <= timestamp,
                        or_(
                            Job.dispatched_at.is_(None),
                            Job.dispatched_at < Job.lease_until,
                            Job.dispatched_at < timestamp - DISPATCH_RECOVERY_MS,
                        ),
                    ),
                )
            )
            .order_by(Job.available_at, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return False
        sender.send_messages(ServiceBusMessage(job.id), timeout=10)
        job.dispatched_at = timestamp
        return True


def dispatch_loop(runtime: Runtime, stop: Event):
    from azure.core.exceptions import AzureError
    from azure.servicebus.exceptions import ServiceBusError
    from sqlalchemy.exc import SQLAlchemyError

    while not stop.is_set():
        try:
            with (
                _client(runtime) as client,
                client.get_queue_sender(runtime.settings.service_bus_queue) as sender,
            ):
                while not stop.is_set():
                    if not _dispatch(runtime, sender):
                        stop.wait(2)
        except (AzureError, ServiceBusError, SQLAlchemyError) as exc:
            logger.warning("queue_dispatch_failed type=%s", type(exc).__name__)
            stop.wait(5)


def consume_message(runtime: Runtime, agent: Agent):
    from azure.servicebus import AutoLockRenewer

    with (
        _client(runtime) as client,
        AutoLockRenewer(max_lock_renewal_duration=WORKER_TIMEOUT_SECONDS + 60) as renewer,
        client.get_queue_receiver(runtime.settings.service_bus_queue, auto_lock_renewer=renewer) as receiver,
    ):
        for message in receiver.receive_messages(max_message_count=1, max_wait_time=5):
            try:
                job_id = str(UUID(str(message)))
            except (ValueError, UnicodeError):
                logger.warning("invalid_queue_message")
                receiver.dead_letter_message(message, reason="InvalidJobId")
                continue
            run_one(runtime, agent, job_id)
            receiver.complete_message(message)
