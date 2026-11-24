import argparse
import logging
from threading import Event, Thread

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from edu_app.agent_client import HttpAgent, build_agent
from edu_app.api import build_app
from edu_app.config import Settings
from edu_app.jobs import run_one
from edu_app.migration import migrate
from edu_app.queue import consume_message
from edu_app.runtime import Runtime

logger = logging.getLogger(__name__)


def local_worker(runtime: Runtime, stop: Event):
    agent = build_agent(runtime.settings, runtime.credential)
    try:
        while not stop.is_set():
            try:
                if not run_one(runtime, agent):
                    stop.wait(0.4)
            except SQLAlchemyError as exc:
                logger.error("local_job_database_failed type=%s", type(exc).__name__)
                stop.wait(2)
    finally:
        if isinstance(agent, HttpAgent):
            agent.close()


def serve(runtime: Runtime, *, port: int, with_worker: bool):
    stop = Event()
    worker = Thread(target=local_worker, args=(runtime, stop), name="local-worker", daemon=True)
    if with_worker:
        worker.start()
    try:
        host = "127.0.0.1" if runtime.settings.mode == "local" else "0.0.0.0"
        uvicorn.run(build_app(runtime), host=host, port=port, proxy_headers=False)
    finally:
        stop.set()
        if with_worker:
            worker.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description="Edu AI application")
    parser.add_argument("command", choices=("migrate", "serve", "worker", "consume"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--with-worker", action="store_true", help="Run the local DB worker with the API.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = Settings()
    if (args.command == "worker" or args.with_worker) and settings.mode != "local":
        parser.error("The database-polling worker is only available in local mode")
    if args.command == "consume" and settings.mode != "azure":
        parser.error("Service Bus consumption requires Azure mode")
    runtime = Runtime(settings)
    try:
        if args.command == "migrate":
            migrate(runtime.db)
        elif args.command == "worker":
            local_worker(runtime, Event())
        elif args.command == "consume":
            agent = build_agent(settings, runtime.credential)
            try:
                consume_message(runtime, agent)
            finally:
                if isinstance(agent, HttpAgent):
                    agent.close()
        else:
            serve(runtime, port=args.port, with_worker=args.with_worker)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
