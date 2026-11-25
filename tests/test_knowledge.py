import json
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from edu_agent.demo import DemoAgent, demo_page
from edu_agent.english_demo import english_demo_page
from edu_agent.service import MathAgent, ModelError
from edu_app.api import build_app
from edu_app.auth import Auth
from edu_app.db import Evidence, Job, KnowledgeActivity, KnowledgePoint
from edu_app.jobs import run_one
from edu_app.migration import migrate
from edu_domain.contracts import (
    DiscoverKnowledge,
    GenerateKnowledge,
    KnowledgeDiscoveries,
    KnowledgeDiscoveryResult,
    KnowledgeFeedbackResult,
    ReferenceExercises,
)
from edu_domain.images import normalize_page

_STUDENT = {"X-Demo-Student": "demo-b"}
_VERSION = {"model": "test-model", "prompt": "knowledge-reference-v1", "agent": "test-agent"}


def _input(subject="math", title="三角形内角和"):
    return {"subject": subject, "title": title, "unit": "几何", "description": "三角形三个内角之和为180度。"}


def _upload(client, subject="math", headers=None):
    path, image = ("/api/submissions", demo_page()) if subject == "math" else (
        "/api/english/worksheets", english_demo_page()
    )
    response = client.post(path, files=[("files", ("sample.png", image, "image/png"))], headers=headers)
    assert response.status_code == 202
    activity = client.get(f"/api/knowledge-activities?subject={subject}", headers=headers).json()[0]
    return response.json(), activity


def _discovery(subject="math", title=None, existing_id=None, quality="readable"):
    title = title or ("三角形内角和" if subject == "math" else "一般过去时")
    return KnowledgeDiscoveryResult(
        version="1", operation="discover_knowledge", quality=quality, provenance=_VERSION,
        items=[{
            "label": "1", "region": {"left": 10, "top": 10, "right": 900, "bottom": 300},
            "stem": "PRIVATE printed exercise",
            "points": [{"existing_id": existing_id, "title": title, "unit": "拓展",
                        "description": "理解并运用这个知识点。"}],
            "feedback": None,
        }],
    )


def test_shared_directory_has_teacher_only_maintenance_and_normalized_duplicates(client, runtime):
    assert client.get("/api/knowledge-points").json()["canManage"]
    assert not client.get("/api/knowledge-points", headers=_STUDENT).json()["canManage"]
    assert client.post("/api/knowledge-points", json=_input(), headers=_STUDENT).status_code == 403
    point = client.post("/api/knowledge-points", json=_input()).json()
    assert point["assessment"] == "reference"
    assert client.post("/api/knowledge-points", json=_input(title=" 三角形 内角和 ")).status_code == 409
    shared = client.get("/api/knowledge-points", headers=_STUDENT).json()["points"]
    assert point in shared and all("created_by" not in item for item in shared)
    path = f"/api/knowledge-points/{point['id']}"
    assert client.patch(path, json=_input(), headers=_STUDENT).status_code == 403
    updated = client.patch(path, json={**_input(), "description": "先识别三个内角，再求和。"}).json()
    assert updated["assessment"] == "reference" and updated["reviewed"]
    assert client.patch("/api/knowledge-points/rational-sum", json=_input()).status_code == 409
    for field in ("title", "unit", "description"):
        assert client.post("/api/knowledge-points", json={**_input(), field: " "}).status_code == 422
    migrate(runtime.db)
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(KnowledgePoint)) == 13


@pytest.mark.parametrize("subject", ["math", "english"])
def test_demo_discovery_completes_with_bounded_catalog_descriptions(client, runtime, subject):
    _, activity = _upload(client, subject)
    assert run_one(runtime, DemoAgent(), activity["jobId"])
    detail = client.get(f"/api/knowledge-activities/{activity['id']}").json()
    assert detail["status"] == "done" and detail["items"]
    assert all(point["origin"] == "builtin" for item in detail["items"] for point in item["points"])


@pytest.mark.parametrize("subject", ["math", "english"])
def test_upload_discovers_once_shares_only_concept_and_preserves_private_source(client, runtime, subject):
    submission, activity = _upload(client, subject)
    assert _upload(client, subject)[0] == submission
    agent = Mock(invoke=Mock(return_value=_discovery(subject)))
    assert run_one(runtime, agent, activity["jobId"])
    assert not run_one(runtime, agent, activity["jobId"])
    agent.invoke.assert_called_once()
    detail_path = f"/api/knowledge-activities/{activity['id']}"
    detail = client.get(detail_path).json()
    assert detail["status"] == "done"
    assert not detail["eligibleForMastery"]
    point = detail["items"][0]["points"][0]
    assert point["origin"] == "discovered" and not point["reviewed"]
    assert point in client.get(f"/api/knowledge-points?subject={subject}", headers=_STUDENT).json()["points"]
    assert "PRIVATE" not in str(point)
    assert client.get(detail_path, headers=_STUDENT).status_code == 404
    assert client.post(f"{detail_path}/retry", headers=_STUDENT).status_code == 404
    assert client.get(f"/api/knowledge-activities?subject={subject}", headers=_STUDENT).json() == []
    _, other = _upload(client, subject, headers=_STUDENT)
    assert run_one(runtime, agent, other["jobId"])
    with runtime.db.session() as session:
        points = list(session.scalars(select(KnowledgePoint).where(KnowledgePoint.origin == "discovered")))
        assert len(points) == 1 and points[0].id == point["id"]
        assert session.scalar(select(func.count()).select_from(Evidence)) == 0


def test_concurrent_discovery_reuses_one_shared_concept(client, runtime):
    _, first = _upload(client)
    _, second = _upload(client, headers=_STUDENT)
    agent = Mock(invoke=Mock(side_effect=lambda _request: _discovery()))
    with ThreadPoolExecutor(2) as threads:
        tasks = [threads.submit(run_one, runtime, agent, item["jobId"]) for item in (first, second)]
        assert all(task.result() for task in tasks)
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(KnowledgePoint).where(
            KnowledgePoint.origin == "discovered"
        )) == 1
        assert all(session.get(Job, activity["jobId"]).status == "done" for activity in (first, second))


def test_unclear_or_forged_discovery_cannot_expand_catalog(client, runtime):
    _, activity = _upload(client)
    agent = Mock(invoke=Mock(return_value=_discovery(existing_id="forged-id")))
    assert run_one(runtime, agent, activity["jobId"])
    with runtime.db.transaction() as session:
        job = session.get(Job, activity["jobId"])
        assert job.status == "queued" and job.error == "agent_invalid_result"
        job.available_at = 0
    agent.invoke.return_value = _discovery(quality="unclear")
    assert run_one(runtime, agent, activity["jobId"])
    detail = client.get(f"/api/knowledge-activities/{activity['id']}").json()
    assert detail["items"] == [] and detail["retakePages"] == [1]
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(KnowledgePoint)) == 12


def test_reference_practice_and_feedback_are_private_idempotent_and_never_scored(client, runtime):
    point = client.post("/api/knowledge-points", json=_input()).json()
    response = client.post(f"/api/knowledge-points/{point['id']}/practices", json={"count": 3})
    assert response.status_code == 202
    activity = response.json()
    assert run_one(runtime, DemoAgent(), activity["jobId"])
    path = f"/api/knowledge-activities/{activity['id']}"
    detail = client.get(path).json()
    assert len(detail["items"]) == 3
    assert all(item["referenceAnswer"] is None for item in detail["items"])
    item = detail["items"][0]
    answer_path = f"{path}/items/{item['id']}/answer"
    assert client.post(answer_path, json={"answer": "180"}, headers=_STUDENT).status_code == 404
    feedback = client.post(answer_path, json={"answer": "180"}).json()
    assert client.post(answer_path, json={"answer": "180"}).json() == feedback
    assert client.post(answer_path, json={"answer": "90"}).status_code == 409
    assert run_one(runtime, DemoAgent(), feedback["jobId"])
    assert not run_one(runtime, DemoAgent(), feedback["jobId"])
    final = client.get(path).json()["items"][0]
    assert final["feedback"] and final["referenceAnswer"]
    assert not client.get(path).json()["eligibleForMastery"]
    assert client.get(path, headers=_STUDENT).status_code == 404
    assert all(item["sampleCount"] == 0 for item in client.get("/api/mastery").json())
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Evidence)) == 0
        assert session.scalar(select(func.count()).select_from(KnowledgeActivity).where(
            KnowledgeActivity.kind == "feedback"
        )) == 1


def test_teacher_permission_comes_only_from_validated_api_token(runtime, monkeypatch):
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tenant, user, api_id, spa_id = (uuid4() for _ in range(4))
    auth = Auth(runtime.settings)
    auth.settings = runtime.settings.model_copy(update={
        "mode": "azure", "entra_tenant_id": tenant, "entra_api_client_id": api_id, "entra_spa_client_id": spa_id,
    })
    auth.jwt, auth.issuer = jwt, f"https://login.microsoftonline.com/{tenant}/v2.0"
    auth.keys = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key()))
    monkeypatch.setattr("edu_app.api.Auth", lambda _settings: auth)
    claims = {
        "exp": int(time()) + 300, "iat": int(time()), "iss": auth.issuer, "aud": str(api_id),
        "tid": str(tenant), "oid": str(user), "azp": str(spa_id), "scp": "access_as_user",
    }
    with TestClient(build_app(runtime)) as client:
        for roles, expected in (([], 403), ("Teacher", 403), (["Teacher"], 201), (["Admin"], 201)):
            token = jwt.encode({**claims, "roles": roles}, private_key, algorithm="RS256")
            response = client.post("/api/knowledge-points", json=_input(title=f"知识点{expected}{roles}"),
                                   headers={"Authorization": f"Bearer {token}", "X-Demo-Student": "demo-a",
                                            "X-Role": "Admin"})
            assert response.status_code == expected
        bad = jwt.encode({**claims, "aud": str(spa_id), "roles": ["Admin"]}, private_key, algorithm="RS256")
        assert client.post("/api/knowledge-points", json=_input(),
                           headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_agent_discovery_rejects_invented_ids_and_reference_generation_validates_count():
    import asyncio

    request = DiscoverKnowledge(
        version="1", operation="discover_knowledge", subject="math", catalog=[],
        image_base64=b64encode(normalize_page(demo_page())).decode("ascii"),
    )
    model = SimpleNamespace(infer=AsyncMock(return_value=(
        KnowledgeDiscoveries.model_validate(_discovery(existing_id="fake-id").model_dump(
            exclude={"operation", "version", "provenance"}
        )), "test-model",
    )))
    with pytest.raises(ModelError, match="model_invalid_result"):
        asyncio.run(MathAgent(model, "test-version").invoke(request))
    assert model.infer.call_args.kwargs["image"].startswith("data:image/jpeg;base64,")
    assert "SHARED catalog" in model.infer.call_args.args[1]
    generation = GenerateKnowledge(
        version="1", operation="generate_knowledge", subject="math", difficulty="foundation", count=4,
        point={"id": uuid4().hex, "title": "三角形内角和", "unit": "几何", "description": ""},
    )
    smaller = generation.model_copy(update={"count": 3})
    model.infer.return_value = (
        ReferenceExercises(items=DemoAgent().invoke(smaller).items), "test-model",
    )
    with pytest.raises(ModelError, match="model_invalid_result"):
        asyncio.run(MathAgent(model, "test-version").invoke(generation))


def test_real_host_contract_supports_reference_feedback_without_content_storage(monkeypatch):
    pytest.importorskip("azure.ai.agentserver.invocations")
    from contextlib import asynccontextmanager

    from edu_agent import host
    from edu_domain.contracts import ReviewKnowledge

    feedback = {"strengths": ["表达完整。"], "improvements": ["说明依据。"], "suggested_response": "结合条件说明推理。"}
    create = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_text=json.dumps(feedback), model="test-model",
    ))

    @asynccontextmanager
    async def credential():
        yield object()

    @asynccontextmanager
    async def project(**_kwargs):
        @asynccontextmanager
        async def openai(**_kwargs):
            yield SimpleNamespace(responses=SimpleNamespace(create=create))
        yield SimpleNamespace(get_openai_client=openai)

    monkeypatch.setattr(host, "AzureCliCredential", credential)
    monkeypatch.setattr(host, "AIProjectClient", project)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://unused.example.invalid")
    monkeypatch.setenv("MODEL_DEPLOYMENT_NAME", "test-model")
    request = ReviewKnowledge(
        version="1", operation="review_knowledge", subject="math", answer="180",
        point={"id": uuid4().hex, "title": "三角形内角和", "unit": "几何", "description": ""},
        exercise={"instruction": "三个内角之和是多少？", "context": "", "reference_answer": "180度",
                  "explanation": "这是未经验证的参考。"},
    )
    with TestClient(host.build_host(local=True)) as client:
        result = client.post("/invocations", json=request.model_dump())
    assert result.status_code == 200
    parsed = KnowledgeFeedbackResult.model_validate(result.json())
    assert parsed.feedback.model_dump() == feedback
    assert "score" not in result.json()
    assert create.call_args.kwargs["store"] is False
