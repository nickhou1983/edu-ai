import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from edu_app.__main__ import serve
from edu_app.config import Settings
from edu_app.migration import migrate
from edu_app.runtime import Runtime

if __name__ == "__main__":
    # Uvicorn replays the signal after shutdown; let the temporary database be removed.
    signal.signal(signal.SIGTERM, lambda *_: None)
    with TemporaryDirectory(prefix="edu-ai-e2e-") as directory:
        runtime = Runtime(
            Settings(
                _env_file=None,
                mode="local",
                data_dir=Path(directory),
                web_dir=Path("apps/web/dist"),
                database_url=None,
                agent_url=None,
            )
        )
        try:
            migrate(runtime.db)
            serve(runtime, port=int(sys.argv[1]), with_worker=True)
        finally:
            runtime.close()
