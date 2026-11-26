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
from edu_agent.service import MathAgent, ModelError
from edu_domain.agent_protocol import JOB_ID_HEADER, PAGE_READ_BUDGET_HEADER
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


@pytest.fixture
def hosted_model(monkeypatch):
    pytest.importorskip("azure.ai.agentserver.invocations")
    from edu_agent import host as agent_host
    response = AsyncMock()
    options = Mock()

    @asynccontextmanager
    async def stub_project(**_kwargs):
        @asynccontextmanager
        async def stub_openai(**kwargs):
            options(**kwargs)
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

    return agent_host, response, options


def _completed(output):
    return SimpleNamespace(status="completed", output_text=output.model_dump_json(), model="test-model")


def test_host_accepts_an_invocation_and_never_enables_content_recording(hosted_model, open_math_item, monkeypatch):
    from edu_domain.contracts import OpenReviews, OpenSolutions

    agent_host, response, options = hosted_model
    monkeypatch.setenv("EDU_MATH_MODEL_TIMEOUT_SECONDS", "95")
    photo = _photo_request()
    assert "review_items" not in photo.model_dump()
    generation = GeneratePractice(
        version="1",
        operation="generate_practice",
        skill="rational-sum",
        purpose="diagnose",
        difficulty="foundation",
        count=3,
    )
    original = demo_analysis().items[0]
    response.side_effect = [
        _completed(output) for output in [
            ReadPage(quality="readable", items=[original.source]),
            SolvePage(solutions=[Candidate(index=0, spec=original.spec, answer=original.candidate)]),
            PracticeCandidates(items=[ProblemSpec(task="evaluate", expression="2-7", choices=[])]),
            ReadPage(quality="readable", items=[open_math_item.source]),
            OpenSolutions(solutions=[open_math_item.assessment.first]),
            OpenSolutions(solutions=[open_math_item.assessment.second]),
            OpenReviews(reviews=[open_math_item.assessment.review]),
        ]
    ]
    with TestClient(agent_host.build_host(local=True)) as client:
        analyzed = client.post("/invocations", json=photo.model_dump())
        assert analyzed.status_code == httpx.codes.OK
        assert analyzed.json()["items"][0]["candidate"] == "-5"
        assert "assessment" not in analyzed.json()["items"][0]
        assert "review_timed_out" not in analyzed.json()["items"][0]
        assert "question" not in analyzed.json()["items"][0]["source"]
        generated = client.post("/invocations", json=generation.model_dump())
        assert generated.status_code == httpx.codes.OK
        assert generated.json()["items"] == [{"task": "evaluate", "expression": "2-7", "choices": []}]
        opened = client.post("/invocations", json=photo.model_copy(update={"version": "2"}).model_dump())
        assert opened.status_code == httpx.codes.OK
        opened_result = AnalysisResult.model_validate_json(opened.content)
        assert opened_result.version == "2"
        assert opened_result.items[0].source.question == open_math_item.source.question
        assert "review_timed_out" not in opened.json()["items"][0]
        assert opened_result.items[0].assessment.first_tool.value == "2"
        invalid = client.post("/invocations", json={**photo.model_dump(), "image_base64": "----"})
        assert invalid.status_code == httpx.codes.UNPROCESSABLE_ENTITY
        oversized = client.post("/invocations", content=b"x" * (agent_host._MAX_BODY + 1))
        assert oversized.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE

    assert response.await_count == 7
    assert [call.kwargs["timeout"] for call in options.call_args_list[:3]] == [45, 45, 95]
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


@pytest.mark.parametrize(("status", "code", "retry_after"), [
    (None, "model_sdk_timeout", None),
    (408, "model_http_timeout", None),
    (429, "model_rate_limited", "120"),
])
def test_model_timeout_and_rate_limits_remain_distinguishable(hosted_model, caplog, status, code, retry_after):
    from openai import APIStatusError, APITimeoutError

    host, create, _ = hosted_model
    request = httpx.Request("POST", "https://model.example.invalid")
    if status is None:
        create.side_effect = APITimeoutError(request=request)
    else:
        response = httpx.Response(status, headers={"retry-after": "180"}, request=request)
        create.side_effect = APIStatusError("private-error-detail", response=response, body=None)
    photo = _photo_request().model_copy(update={"version": "2"})
    job_id = "00000000-0000-4000-8000-000000000001"

    with TestClient(host.build_host(local=True)) as client:
        result = client.post("/invocations", json=photo.model_dump(), headers={JOB_ID_HEADER: job_id})

    assert result.status_code == 503
    assert result.json() == {"error": code}
    assert result.headers.get("Retry-After") == retry_after
    create.assert_awaited_once()
    assert f"job_id={job_id} operation=analyze_submission stage=ReadPage elapsed_ms=" in caplog.text
    assert "private-error-detail" not in caplog.text


def test_math_page_deadline_cancels_stalled_read(hosted_model, monkeypatch):
    host, create, _ = hosted_model
    monkeypatch.setenv("EDU_MATH_PAGE_TIMEOUT_SECONDS", "1")
    cancelled = False

    async def stalled(**_kwargs):
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        finally:
            cancelled = True

    create.side_effect = stalled
    with TestClient(host.build_host(local=True)) as client:
        result = client.post(
            "/invocations", json=_photo_request().model_copy(update={"version": "2"}).model_dump(),
            headers={PAGE_READ_BUDGET_HEADER: "600"},
        )
    assert result.status_code == 503
    assert result.json() == {"error": "model_page_deadline"}
    assert cancelled


def test_open_review_timeout_keeps_verified_results_in_report(
    hosted_model, client, runtime, open_math_item, caplog,
):
    from openai import APITimeoutError

    from edu_app.agent_client import HttpAgent
    from edu_app.config import Settings
    from edu_app.db import Evidence
    from edu_app.jobs import run_one
    from edu_domain.contracts import OpenSolutions

    host, create, options = hosted_model
    open_items = [open_math_item.source.model_copy(update={"label": str(i + 2)}) for i in range(12)]
    rule = demo_analysis().items[0]
    question = open_math_item.source.question.model_copy(update={
        "text": rule.source.stem, "goal": "Calculate", "conditions": [], "parts": [], "points": [],
    })
    source = rule.source.model_copy(update={"question": question})
    solutions = OpenSolutions(solutions=[
        open_math_item.assessment.first.model_copy(update={"index": i + 1}) for i in range(len(open_items))
    ])
    timeout = APITimeoutError(request=httpx.Request("POST", "https://model.example.invalid"))
    create.side_effect = [
        _completed(ReadPage(quality="readable", items=[source, *open_items])),
        _completed(SolvePage(solutions=[Candidate(index=0, spec=rule.spec, answer=rule.candidate)])),
        _completed(solutions), _completed(solutions), timeout, timeout,
    ]
    uploaded = client.post("/api/submissions", files=[("files", ("work.png", demo_page(), "image/png"))])
    assert uploaded.status_code == 202
    submission = uploaded.json()
    wait_budgets = []
    with TestClient(host.build_host(local=True)) as remote:
        def forward(request):
            wait_budgets.append(request.headers[PAGE_READ_BUDGET_HEADER])
            result = remote.post(request.url.path, content=request.read(), headers=dict(request.headers))
            return httpx.Response(result.status_code, headers=result.headers, content=result.content)

        settings = Settings(_env_file=None, mode="local", agent_url="http://127.0.0.1:8080/invocations")
        agent = HttpAgent(settings, None)
        agent.client.close()
        agent.client = httpx.Client(transport=httpx.MockTransport(forward), timeout=600)
        try:
            assert run_one(runtime, agent, submission["jobId"])
        finally:
            agent.close()

    detail = client.get(f"/api/submissions/{submission['id']}").json()
    assert detail["status"] == "done"
    assert detail["report"]["retakePages"] == []
    verified, *pending = detail["report"]["items"]
    assert (verified["outcome"], verified["eligibleForMastery"]) == ("correct", True)
    assert len(pending) == 12
    assert all(
        (row["outcome"], row["reason"], row["eligibleForMastery"]) == ("undetermined", "review_timeout", False)
        and row["expected"] is None and not row["stepFeedback"] for row in pending
    )
    with runtime.db.session() as session:
        assert {entry.source_id for entry in session.query(Evidence).all()} == {verified["id"]}
    assert wait_budgets == ["600"]
    options.assert_called_once_with(timeout=90, max_retries=0)
    assert [call.kwargs["text"]["format"]["name"] for call in create.await_args_list] == [
        "ReadPage", "SolvePage", "OpenSolutions", "OpenSolutions", "OpenReviews", "OpenReviews",
    ]
    assert f"job_id={submission['jobId']} operation=analyze_submission stage=OpenReviews" in caplog.text


def test_all_pending_report_retries_through_host_without_rereading_page(
    hosted_model, client, runtime, open_math_item,
):
    from edu_app.agent_client import HttpAgent
    from edu_app.config import Settings
    from edu_app.db import Evidence
    from edu_app.jobs import run_one
    from edu_domain.contracts import OpenReviews, OpenSolutions

    host, create, options = hosted_model
    assessment = open_math_item.assessment
    create.side_effect = [
        _completed(OpenSolutions(solutions=[assessment.first])),
        _completed(OpenSolutions(solutions=[assessment.second])),
        _completed(OpenReviews(reviews=[assessment.review])),
    ]
    upload = client.post("/api/submissions", files=[("files", ("work.png", demo_page(), "image/png"))]).json()
    path = f"/api/submissions/{upload['id']}"
    initial = demo_analysis().model_copy(update={
        "version": "2",
        "items": [open_math_item.model_copy(update={"assessment": None, "review_timed_out": True})],
    })
    initial_agent = Mock()
    initial_agent.invoke.return_value = initial
    assert run_one(runtime, initial_agent, upload["jobId"])
    before = client.get(path).json()
    assert before["status"] == "done" and before["canRetry"]
    assert client.post(f"{path}/retry").status_code == 202
    assert client.get(path).json()["report"] == before["report"]
    original_image = initial_agent.invoke.call_args.args[0].image_base64
    with TestClient(host.build_host(local=True)) as remote:
        def forward(request):
            payload = AnalyzePage.model_validate_json(request.read())
            assert payload.image_base64 == original_image
            assert payload.review_items == [open_math_item.source]
            assert request.headers[PAGE_READ_BUDGET_HEADER] == "600"
            result = remote.post(request.url.path, content=request.content, headers=dict(request.headers))
            return httpx.Response(result.status_code, headers=result.headers, content=result.content)

        agent = HttpAgent(
            Settings(_env_file=None, mode="local", agent_url="http://127.0.0.1:8080/invocations"), None,
        )
        agent.client.close()
        agent.client = httpx.Client(transport=httpx.MockTransport(forward), timeout=600)
        try:
            assert run_one(runtime, agent, upload["jobId"])
        finally:
            agent.close()

    updated = client.get(path).json()
    assert (updated["status"], updated["canRetry"], updated["revision"]) == ("done", False, 2)
    item = updated["report"]["items"][0]
    assert item["id"] == before["report"]["items"][0]["id"]
    assert (item["verification"], item["expected"], item["eligibleForMastery"]) == ("ai_review", "2", False)
    with runtime.db.session() as session:
        assert session.query(Evidence).count() == 0
    options.assert_called_once_with(timeout=90, max_retries=0)
    inputs = [call.kwargs["input"][0]["content"] for call in create.await_args_list]
    assert [call.kwargs["text"]["format"]["name"] for call in create.await_args_list] == [
        "OpenSolutions", "OpenSolutions", "OpenReviews",
    ]
    for blind in inputs[:2]:
        assert [part["type"] for part in blind] == ["input_text"]
        assert set(json.loads(blind[0]["text"])[0]) == {"index", "question", "choices"}
    assert json.loads(inputs[2][0]["text"])[0]["student_answer"] == open_math_item.source.answer
    assert inputs[2][1]["image_url"] == f"data:image/jpeg;base64,{original_image}"


def test_legacy_requests_do_not_return_expanded_answer_fields():
    source = demo_analysis().items[0].source.model_copy(update={"answer": "7" * 97})
    model = SimpleNamespace(infer=AsyncMock(return_value=(
        ReadPage(quality="readable", items=[source]), "test",
    )))
    with pytest.raises(ModelError, match="model_invalid_result"):
        asyncio.run(MathAgent(model, "test").invoke(_photo_request()))
    model.infer.assert_awaited_once()


def test_open_requests_require_full_question_before_any_scoring():
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
