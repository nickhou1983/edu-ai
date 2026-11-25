import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def test_isolated_app_release_contains_english_runtime_imports(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("build_context", root / "infra/release/build_context.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    destination = tmp_path / "release"
    release.create_contexts(root, destination, ("app",))
    app = destination / "app"
    code = """
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
sys.path[:0] = [str(root / name) for name in (
    "services/app", "services/foundry-agent", "packages/domain",
)]
import edu_app.api
import edu_app.jobs
import edu_agent.english_demo
import edu_domain.english
for module in (edu_app.api, edu_app.jobs, edu_agent.english_demo, edu_domain.english):
    assert pathlib.Path(module.__file__).is_relative_to(root), module.__file__
"""
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("EDU_", "PYTHON"))}
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(app)],
        cwd=tmp_path, env=environment, check=True, capture_output=True, text=True,
    )
