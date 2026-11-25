import asyncio
import json
import os
from base64 import b64encode
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.testclient import TestClient

from edu_agent.demo import demo_analysis, demo_page
from edu_agent.service import MathAgent
from edu_domain.contracts import (
    MAX_PAGE_BYTES,
    AnalysisResult,
    AnalyzePage,
    Candidate,
    GeneratePractice,
    PracticeCandidates,
    ProblemSpec,
    ReadPage,
    SolvePage,
)
from edu_domain.images import normalize_page


def _photo_request():
    return AnalyzePage(
        version="1",
        operation="analyze_submission",
        image_base64=b64encode(normalize_page(demo_page())).decode("ascii"),
    )


def test_agent_solves_printed_questions_without_student_work_or_photo():
    first, second, *rest = demo_analysis().items
    source = first.source.model_copy(update={"answer": "31415926", "steps": ["12345678=12345678"]})
    unreadable = rest[0].source.model_copy(update={"readable": False})
    model = SimpleNamespace(
        infer=AsyncMock(
            side_effect=[
                (ReadPage(quality="readable", items=[source, second.source, unreadable]), "demo-reader"),
                (
                    SolvePage(
                        solutions=[
                            Candidate(index=0, spec=first.spec, answer=first.candidate),
                            Candidate(index=1, spec=second.spec, answer=second.candidate),
                            Candidate(index=1, spec=second.spec, answer="2"),
                            Candidate(index=2, spec=rest[0].spec, answer=rest[0].candidate),
                        ]
                    ),
                    "demo-solver",
                ),
            ]
        )
    )
    result = asyncio.run(MathAgent(model, "agent-test").invoke(_photo_request()))
    assert isinstance(result, AnalysisResult)
    assert [item.candidate for item in result.items] == ["-5", None, None]
    read_call, solve_call = model.infer.await_args_list
    assert read_call.kwargs["image"].startswith("data:image/jpeg;base64,")
    assert not solve_call.kwargs
    assert json.loads(solve_call.args[2]) == [
        {"index": 0, "intent": "evaluate", "expression": "-8+3", "choices": []},
        {"index": 1, "intent": "evaluate", "expression": "5-(-3)", "choices": []},
    ]


def test_agent_does_not_guess_at_an_unclear_page():
    model = SimpleNamespace(
        infer=AsyncMock(return_value=(ReadPage(quality="unclear", items=[]), "test-model"))
    )
    result = asyncio.run(MathAgent(model, "agent-test").invoke(_photo_request()))
    assert result.quality == "unclear"
    assert result.items == []
    model.infer.assert_awaited_once()


def test_host_accepts_an_invocation_and_never_enables_content_recording(monkeypatch):
    pytest.importorskip("azure.ai.agentserver.invocations")
    from edu_agent import host as agent_host

    photo = _photo_request()
    generation = GeneratePractice(
        version="1",
        operation="generate_practice",
        skill="rational-sum",
        purpose="diagnose",
        difficulty="foundation",
        count=3,
    )
    original = demo_analysis().items[0]
    response = AsyncMock(
        side_effect=[
            SimpleNamespace(
                status="completed",
                output_text=ReadPage(quality="readable", items=[original.source]).model_dump_json(),
                model="test-model",
            ),
            SimpleNamespace(
                status="completed",
                output_text=SolvePage(
                    solutions=[
                        Candidate(index=0, spec=original.spec, answer=original.candidate),
                    ]
                ).model_dump_json(),
                model="test-model",
            ),
            SimpleNamespace(
                status="completed",
                output_text=PracticeCandidates(
                    items=[
                        ProblemSpec(task="evaluate", expression="2-7", choices=[]),
                    ]
                ).model_dump_json(),
                model="test-model",
            ),
        ]
    )

    @asynccontextmanager
    async def stub_project(**_kwargs):
        @asynccontextmanager
        async def stub_openai(**_options):
            yield SimpleNamespace(responses=SimpleNamespace(create=response))

        yield SimpleNamespace(get_openai_client=stub_openai)

    @asynccontextmanager
    async def stub_credential():
        yield object()

    monkeypatch.setattr(agent_host, "AIProjectClient", stub_project)
    monkeypatch.setattr(agent_host, "AzureCliCredential", stub_credential)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example.invalid")
    monkeypatch.setenv("MODEL_DEPLOYMENT_NAME", "test-deployment")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    monkeypatch.setenv("AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED", "true")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    with TestClient(agent_host.build_host(local=True)) as client:
        analyzed = client.post("/invocations", json=photo.model_dump())
        assert analyzed.status_code == httpx.codes.OK
        assert analyzed.json()["items"][0]["candidate"] == "-5"
        generated = client.post("/invocations", json=generation.model_dump())
        assert generated.status_code == httpx.codes.OK
        assert generated.json()["items"] == [{"task": "evaluate", "expression": "2-7", "choices": []}]
        invalid = client.post("/invocations", json={**photo.model_dump(), "image_base64": "----"})
        assert invalid.status_code == httpx.codes.UNPROCESSABLE_ENTITY
        oversized = client.post("/invocations", content=b"x" * (MAX_PAGE_BYTES * 2))
        assert oversized.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE

    assert response.await_count == 3
    for call in response.await_args_list:
        assert call.kwargs["store"] is False
        assert call.kwargs["text"]["format"]["strict"] is True
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"
    assert os.environ["AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED"] == "false"


def test_host_does_not_start_sdk_model_instrumentation_or_content_export(monkeypatch):
    pytest.importorskip("azure.ai.agentserver.invocations")
    import microsoft.opentelemetry

    from edu_agent import host as agent_host

    setup = Mock(side_effect=AssertionError("Automatic model instrumentation must not start"))
    monkeypatch.setattr(microsoft.opentelemetry, "use_microsoft_opentelemetry", setup)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example.invalid")
    monkeypatch.setenv("MODEL_DEPLOYMENT_NAME", "test-deployment")

    agent_host.build_host()

    setup.assert_not_called()
