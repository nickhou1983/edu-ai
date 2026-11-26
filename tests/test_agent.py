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


def test_host_accepts_an_invocation_and_never_enables_content_recording(monkeypatch, open_math_item):
    pytest.importorskip("azure.ai.agentserver.invocations")
    from edu_agent import host as agent_host
    from edu_domain.contracts import OpenReviews, OpenSolutions

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
            *[
                SimpleNamespace(status="completed", output_text=output.model_dump_json(), model="test-model")
                for output in [
                    ReadPage(quality="readable", items=[open_math_item.source]),
                    OpenSolutions(solutions=[open_math_item.assessment.first]),
                    OpenSolutions(solutions=[open_math_item.assessment.second]),
                    OpenReviews(reviews=[open_math_item.assessment.review]),
                ]
            ],
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
        assert "assessment" not in analyzed.json()["items"][0]
        assert "question" not in analyzed.json()["items"][0]["source"]
        generated = client.post("/invocations", json=generation.model_dump())
        assert generated.status_code == httpx.codes.OK
        assert generated.json()["items"] == [{"task": "evaluate", "expression": "2-7", "choices": []}]
        opened = client.post("/invocations", json=photo.model_copy(update={"version": "2"}).model_dump())
        assert opened.status_code == httpx.codes.OK
        opened_result = AnalysisResult.model_validate_json(opened.content)
        assert opened_result.version == "2"
        assert opened_result.items[0].source.question == open_math_item.source.question
        assert opened_result.items[0].assessment.first_tool.value == "2"
        invalid = client.post("/invocations", json={**photo.model_dump(), "image_base64": "----"})
        assert invalid.status_code == httpx.codes.UNPROCESSABLE_ENTITY
        oversized = client.post("/invocations", content=b"x" * (agent_host._MAX_BODY + 1))
        assert oversized.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE

    assert response.await_count == 7
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


def test_open_agent_blind_solvers_never_receive_student_answers(open_math_item):
    from edu_domain.contracts import OpenReviews, OpenSolutions
    from edu_domain.grading import grade_item

    source = open_math_item.source.model_copy(update={"answer": "student_private_987654"})
    assessment = open_math_item.assessment
    model = SimpleNamespace(infer=AsyncMock(side_effect=[
        (ReadPage(quality="readable", items=[source]), "test-model"),
        (OpenSolutions(solutions=[assessment.first]), "test-model"),
        (OpenSolutions(solutions=[assessment.second]), "test-model"),
        (OpenReviews(reviews=[assessment.review]), "test-model"),
    ]))
    result = asyncio.run(MathAgent(model, "agent-test").invoke(_photo_request().model_copy(update={"version": "2"})))
    assert grade_item(result.items[0]).reference == "2"
    assert result.version == "2"
    calls = model.infer.await_args_list
    assert len(calls) == 4
    for call in calls[1:3]:
        assert "student_private_987654" not in call.args[2]
        assert not call.kwargs
        assert json.loads(call.args[2])[0]["question"]["conditions"] == ["a=-1", "b=1/3"]
    assert "student_private_987654" in calls[3].args[2]
    assert calls[3].kwargs["image"].startswith("data:image/jpeg;base64,")


def test_open_agent_drops_duplicate_solver_results(open_math_item):
    from edu_domain.contracts import OpenSolutions

    solution = open_math_item.assessment.first
    model = SimpleNamespace(infer=AsyncMock(side_effect=[
        (ReadPage(quality="readable", items=[open_math_item.source]), "test-model"),
        (OpenSolutions(solutions=[solution, solution]), "test-model"),
        (OpenSolutions(solutions=[solution]), "test-model"),
    ]))
    result = asyncio.run(MathAgent(model, "agent-test").invoke(_photo_request().model_copy(update={"version": "2"})))
    assert result.items[0].assessment is None
    assert model.infer.await_count == 3


def test_model_respects_rate_limit_retry_after(monkeypatch):
    pytest.importorskip("openai")
    from openai import RateLimitError

    from edu_agent import host

    response = httpx.Response(
        429, headers={"retry-after": "2"}, request=httpx.Request("POST", "https://model.example.invalid"),
    )
    create = AsyncMock(side_effect=[
        RateLimitError("quota", response=response, body=None),
        SimpleNamespace(status="completed", output_text='{"quality":"unclear","items":[]}', model="test"),
    ])
    sleep = AsyncMock()
    monkeypatch.setattr(host.asyncio, "sleep", sleep)
    model = host.FoundryModel(SimpleNamespace(responses=SimpleNamespace(create=create)), "test")
    result, _ = asyncio.run(model.infer(ReadPage, "read", "sample"))
    assert result.quality == "unclear"
    sleep.assert_awaited_once_with(2)
    assert create.await_count == 2


def test_legacy_requests_do_not_return_expanded_answer_fields():
    from edu_agent.service import ModelError

    source = demo_analysis().items[0].source.model_copy(update={"answer": "7" * 97})
    model = SimpleNamespace(infer=AsyncMock(return_value=(
        ReadPage(quality="readable", items=[source]), "test",
    )))
    with pytest.raises(ModelError, match="model_invalid_result"):
        asyncio.run(MathAgent(model, "test").invoke(_photo_request()))
    model.infer.assert_awaited_once()


def test_open_requests_require_full_question_before_any_scoring():
    from edu_agent.service import ModelError

    original = demo_analysis().items[0]
    model = SimpleNamespace(infer=AsyncMock(side_effect=[
        (ReadPage(quality="readable", items=[original.source]), "test"),
        (SolvePage(solutions=[Candidate(index=0, spec=original.spec, answer=original.candidate)]), "test"),
    ]))
    with pytest.raises(ModelError, match="model_invalid_result"):
        asyncio.run(MathAgent(model, "test").invoke(_photo_request().model_copy(update={"version": "2"})))
    model.infer.assert_awaited_once()


def test_full_question_conditions_do_not_use_single_expression_grading(open_math_item):
    original = demo_analysis().items[0]
    question = open_math_item.source.question.model_copy(update={
        "text": "计算-8+3，再求这个结果的相反数。", "goal": "求计算结果的相反数",
        "conditions": [], "parts": ["计算", "求相反数"], "points": [],
    })
    source = original.source.model_copy(update={"question": question})
    from edu_domain.contracts import OpenSolutions

    model = SimpleNamespace(infer=AsyncMock(side_effect=[
        (ReadPage(quality="readable", items=[source]), "test"),
        (OpenSolutions(solutions=[]), "test"),
        (OpenSolutions(solutions=[]), "test"),
    ]))
    result = asyncio.run(MathAgent(model, "test").invoke(_photo_request().model_copy(update={"version": "2"})))
    assert result.items[0].spec is None
    assert all(call.args[0] is OpenSolutions for call in model.infer.await_args_list[1:])
