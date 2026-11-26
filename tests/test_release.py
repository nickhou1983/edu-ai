import importlib.util
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def hosted_release():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("hosted_release", root / "infra/release/hosted_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hosted_release(monkeypatch, release, image, latest, version):
    project = MagicMock()
    project.agents.get.side_effect = [
        SimpleNamespace(versions=SimpleNamespace(latest=latest)),
        SimpleNamespace(state="enabled", versions=SimpleNamespace(latest=SimpleNamespace(version=version))),
    ]
    project.agents.create_version.return_value = SimpleNamespace(version=version)
    project.agents.get_version.return_value = SimpleNamespace(status="active")
    monkeypatch.setattr(release, "AzureCliCredential", lambda: nullcontext(object()))
    monkeypatch.setattr(release, "AIProjectClient", lambda **kwargs: nullcontext(project))
    monkeypatch.setattr(sys, "argv", ["hosted_agent.py", image])
    release.main()
    return project


def test_hosted_release_creates_version_for_same_image_with_changed_model(monkeypatch, hosted_release):
    image = f"{hosted_release.SETTINGS['registry']}.azurecr.io/edu-agent@sha256:{'a' * 64}"
    latest = SimpleNamespace(
        version="5",
        metadata={"image": image},
        definition=hosted_release.definition(image, "previous-model"),
    )
    project = run_hosted_release(monkeypatch, hosted_release, image, latest, "6")
    project.agents.create_version.assert_called_once()
    config = project.agents.create_version.call_args.kwargs["definition"]
    assert config.container_configuration.image == image
    assert (
        config.environment_variables["MODEL_DEPLOYMENT_NAME"]
        == hosted_release.SETTINGS["agentModel"]["deployment"]
    )


def test_hosted_release_reuses_identical_image_and_model(monkeypatch, hosted_release):
    image = f"{hosted_release.SETTINGS['registry']}.azurecr.io/edu-agent@sha256:{'a' * 64}"
    latest = SimpleNamespace(version="6", metadata={"image": image}, definition=hosted_release.definition(image))
    latest.definition["container_protocol_versions"] = []
    project = run_hosted_release(monkeypatch, hosted_release, image, latest, "6")
    project.agents.create_version.assert_not_called()


def test_hosted_release_model_override_preserves_other_configuration(hosted_release):
    image = f"{hosted_release.SETTINGS['registry']}.azurecr.io/edu-agent@sha256:{'a' * 64}"
    config = hosted_release.definition(image).as_dict()
    rollback = hosted_release.definition(image, "previous-model").as_dict()
    assert rollback["environment_variables"].pop("MODEL_DEPLOYMENT_NAME") == "previous-model"
    config["environment_variables"].pop("MODEL_DEPLOYMENT_NAME")
    assert rollback == config


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
import edu_agent.knowledge_demo
import edu_domain.english
import edu_app.knowledge_api
from edu_domain.contracts import Calculation
from edu_domain.symbolic import calculate
for module in (edu_app.api, edu_app.jobs, edu_agent.english_demo, edu_agent.knowledge_demo,
               edu_app.knowledge_api, edu_domain.english):
    assert pathlib.Path(module.__file__).is_relative_to(root), module.__file__
request = Calculation(operation="evaluate", expression="(-1)^1000001", variables=[], bindings=[], comparison=None)
assert calculate(request).value == "-1"
"""
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("EDU_", "PYTHON"))}
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(app)],
        cwd=tmp_path, env=environment, check=True, capture_output=True, text=True,
    )


def test_test_access_verifier_allows_manager_roles_without_expanding_nominated_users(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("entra_release", root / "infra/release/entra.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    users = ["user-one", "user-two"]
    teacher_role = "teacher-role"
    monkeypatch.setattr(release, "find", lambda *_: [{"id": "api-sp"}])
    monkeypatch.setattr(release, "graph", lambda *_: {"appRoleAssignmentRequired": True})
    assignments = [
        {"principalId": users[0], "principalType": "User", "appRoleId": teacher_role},
        {"principalId": users[1], "principalType": "User", "appRoleId": release.DEFAULT_ROLE},
    ]
    monkeypatch.setattr(release, "collection", lambda *_args, **_kwargs: assignments)
    release.verify_assignments({"appId": "test-api"}, users, (release.DEFAULT_ROLE, teacher_role))
    with pytest.raises(RuntimeError, match="two nominated users"):
        release.verify_assignments({"appId": "test-spa"}, users)
    assignments.append({"principalId": "third-user", "principalType": "User", "appRoleId": teacher_role})
    with pytest.raises(RuntimeError, match="two nominated users"):
        release.verify_assignments({"appId": "test-api"}, users, (release.DEFAULT_ROLE, teacher_role))
    assignments.pop()
    assignments[0]["principalType"] = "Group"
    with pytest.raises(RuntimeError, match="two nominated users"):
        release.verify_assignments({"appId": "test-api"}, users, (release.DEFAULT_ROLE, teacher_role))
