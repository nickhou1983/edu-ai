import asyncio
import json
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text

from edu_agent.demo import DemoAgent
from edu_agent.english_demo import DEMO_FEEDBACK, demo_problems, english_demo, english_demo_page
from edu_agent.service import MathAgent
from edu_app.agent_client import AgentFailure, HttpAgent
from edu_app.config import Settings
from edu_app.db import EnglishRecord, Evidence, Job
from edu_app.jobs import run_one
from edu_domain.contracts import (
    AnalyzeEnglish,
    EnglishCandidates,
    EnglishFeedback,
    EnglishPrompt,
    EnglishReadPage,
    EnglishReviewEntry,
    EnglishReviews,
    EnglishSolution,
    EnglishSolutions,
    GenerateEnglish,
    ReviewEnglish,
)
from edu_domain.english import ENGLISH_SKILLS, english_correct, english_outcome
from edu_domain.images import normalize_page

_OTHER = {"X-Demo-Student": "demo-b"}


def _request(skill="english-vocabulary"):
    return GenerateEnglish(
        version="1", operation="generate_english", skill=skill, difficulty="foundation", count=3,
    )


def _photo():
    return AnalyzeEnglish(
        version="1", operation="analyze_english",
        image_base64=b64encode(normalize_page(english_demo_page())).decode("ascii"),
    )


def _upload(client, image=None):
    response = client.post("/api/english/worksheets", files=[
        ("files", ("english.png", image or english_demo_page(), "image/png")),
    ])
    assert response.status_code == 202, response.text
    return response.json()


def _practice(client, runtime, skill="english-vocabulary"):
    response = client.post("/api/english/practices", json={"skill": skill, "count": 3})
    assert response.status_code == 202, response.text
    created = response.json()
    assert run_one(runtime, DemoAgent(), created["jobId"])
    return client.get(f"/api/english/records/{created['id']}").json()


def _evidence_count(runtime):
    with runtime.db.session() as session:
        return session.scalar(select(func.count()).select_from(Evidence))


def test_english_grading_preserves_case_punctuation_and_requires_a_single_choice():
    problem = demo_problems(_request())[0]
    assert english_correct(problem, "  ａｐｐｌｅ \n")
    assert not english_correct(problem, "Apple")
    assert not english_correct(problem, "apple.")
    assert english_outcome(problem, None, "unclear") == "undetermined"
    assert english_outcome(problem, None, "blank") == "blank"
    choice = demo_problems(_request("english-grammar"))[0]
    assert english_correct(choice, "a")
    with pytest.raises(ValidationError):
        type(choice).model_validate({**choice.model_dump(), "answers": ["AB"]})
    reading = demo_problems(_request("english-reading"))[0]
    with pytest.raises(ValidationError):
        type(reading).model_validate({**reading.model_dump(), "context": ""})
    writing = demo_problems(_request("english-writing"))[0]
    with pytest.raises(ValueError):
        english_correct(writing, "I like English.")
    with pytest.raises(ValidationError):
        EnglishFeedback.model_validate({**DEMO_FEEDBACK.model_dump(), "score": 90})


def test_english_upload_replay_private_images_and_subject_isolation(client, runtime):
    assert client.get("/api/english/skills", headers={"X-Demo-Student": ""}).status_code == 401
    record = _upload(client)
    assert _upload(client) == record
    path = f"/api/english/records/{record['id']}"
    assert client.get(path).json()["items"] == []
    agent = Mock(spec=DemoAgent, wraps=DemoAgent())
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(run_one, runtime, agent, record["jobId"]) for _ in range(2)]
        assert sorted(result.result() for result in results) == [False, True]
    assert not run_one(runtime, agent, record["jobId"])
    agent.invoke.assert_called_once()
    detail = client.get(path).json()
    assert detail["status"] == "done"
    assert [item["outcome"] for item in detail["items"]] == ["correct", "incorrect", "correct", "feedback"]
    assert _evidence_count(runtime) == 3
    assert "problem" not in detail["items"][0]
    assert detail["pages"] == [1]
    original = client.get(f"{path}/pages/1")
    assert original.status_code == 200 and original.content.startswith(b"\xff\xd8\xff")
    assert original.headers["cache-control"] == "private, no-store"
    for private in (path, f"{path}/pages/1"):
        assert client.get(private, headers=_OTHER).status_code == 404
    assert client.post(f"{path}/retry", headers=_OTHER).status_code == 404
    assert client.get("/api/english/records", headers=_OTHER).json() == []
    english = client.get("/api/english/skills").json()
    assert [s["sampleCount"] for s in english] == [1, 1, 1, 0, 0, 0]
    assert all(s["status"] == "feedback_only" and s["percent"] is None for s in english[3:])
    assert all(s["sampleCount"] == 0 for s in client.get("/api/mastery").json())


@pytest.mark.parametrize("uncertainty", ["page", "item", "mark", "solution"])
def test_ambiguous_english_does_not_create_mastery(client, runtime, uncertainty):
    created = _upload(client)
    result = english_demo(_photo())
    items = result.items[:3]
    if uncertainty == "page":
        result = result.model_copy(update={"quality": "unclear", "items": items})
    else:
        items = [item.model_copy(update={
            "source": item.source.model_copy(update={
                **({"readable": False} if uncertainty == "item" else {}),
                **({"mark": "unclear"} if uncertainty == "mark" else {}),
            }),
            **({"problem": None} if uncertainty == "solution" else {}),
        }) for item in items]
        result = result.model_copy(update={"items": items})
    assert run_one(runtime, Mock(invoke=Mock(return_value=result)), created["jobId"])
    detail = client.get(f"/api/english/records/{created['id']}").json()
    assert all(item["outcome"] == "undetermined" for item in detail["items"])
    assert _evidence_count(runtime) == 0


@pytest.mark.parametrize("skill", [s.id for s in ENGLISH_SKILLS[:3]])
def test_objective_english_hides_keys_and_counts_each_problem_once(client, runtime, skill):
    record = _practice(client, runtime, skill)
    assert record["status"] == "done" and len(record["items"]) == 3
    assert all(item["expected"] is None and item["explanation"] is None for item in record["items"])
    assert all("problem" not in item and "answers" not in item for item in record["items"])
    problems = demo_problems(_request(skill))
    for item, problem in zip(record["items"], problems, strict=True):
        path = f"/api/english/records/{record['id']}/items/{item['id']}/answer"
        assert client.post(path, headers=_OTHER, json={"answer": problem.answers[0]}).status_code == 404
        assert client.post(path, json={"answer": "  "}).status_code == 422
        if problem.mode == "choice":
            assert client.post(path, json={"answer": "AB"}).status_code == 422
        response = client.post(path, json={"answer": problem.answers[0]})
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "correct"
        assert response.json()["expected"] == problem.answers
        assert client.post(path, json={"answer": problem.answers[0]}).json() == response.json()
        assert client.post(path, json={"answer": "different"}).status_code == 409
    assert _evidence_count(runtime) == 3
    second = _practice(client, runtime, skill)
    for item, problem in zip(second["items"], problems, strict=True):
        assert client.post(f"/api/english/records/{second['id']}/items/{item['id']}/answer",
                           json={"answer": problem.answers[0]}).status_code == 200
    result = next(s for s in client.get("/api/english/skills").json() if s["id"] == skill)
    assert (result["sampleCount"], result["percent"]) == (3, 100)
    assert all(e["englishId"] == record["id"] for e in result["evidence"])


@pytest.mark.parametrize("skill", [s.id for s in ENGLISH_SKILLS[3:]])
def test_subjective_english_uses_durable_idempotent_feedback_without_scores(client, runtime, skill):
    record = _practice(client, runtime, skill)
    item = record["items"][0]
    assert item["prompt"]["mode"] == "open"
    if skill == "english-listening-text":
        assert item["prompt"]["context"]
    path = f"/api/english/records/{record['id']}/items/{item['id']}/answer"
    response = client.post(path, json={"answer": "I like English."})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["counted"] is False
    assert result["feedbackStatus"] == "queued" and result["expected"] == []
    assert client.post(path, json={"answer": "I like English."}).json() == result
    with runtime.db.session() as session:
        job = session.scalar(select(Job).where(Job.english_id == result["feedbackId"]))
        assert session.scalar(select(func.count()).select_from(EnglishRecord)) == 2
        assert run_one(runtime, DemoAgent(), job.id)
        assert not run_one(runtime, DemoAgent(), job.id)
    detail = client.get(f"/api/english/records/{record['id']}").json()
    assert detail["items"][0]["feedbackStatus"] == "done"
    assert detail["items"][0]["feedback"] == DEMO_FEEDBACK.model_dump()
    assert client.get(f"/api/english/records/{result['feedbackId']}", headers=_OTHER).status_code == 404
    assert _evidence_count(runtime) == 0
    assert all(s["sampleCount"] == 0 for s in client.get("/api/english/skills").json())


def test_demo_rejects_arbitrary_photos_and_uploads_are_atomic(client, runtime):
    output = BytesIO()
    Image.new("RGB", (192, 192), "white").save(output, "PNG")
    image = output.getvalue()
    response = client.post("/api/english/worksheets", files=[
        ("files", ("valid.png", english_demo_page(), "image/png")),
        ("files", ("invalid.png", b"not an image", "image/png")),
    ])
    assert response.status_code == 422
    assert client.get("/api/english/records").json() == []
    record = _upload(client, image)
    assert run_one(runtime, DemoAgent(), record["jobId"])
    detail = client.get(f"/api/english/records/{record['id']}").json()
    assert detail["status"] == "failed" and detail["canRetry"] is False
    assert "合成样卷" in detail["error"]
    assert detail["items"] == [] and _evidence_count(runtime) == 0


def test_english_feedback_failure_is_visible_and_retryable(client, runtime):
    record = _practice(client, runtime, "english-writing")
    item = client.post(f"/api/english/records/{record['id']}/items/{record['items'][0]['id']}/answer",
                       json={"answer": "I like English."}).json()
    with runtime.db.transaction() as session:
        job = session.scalar(select(Job).where(Job.english_id == item["feedbackId"]))
        job.attempts = 2
        job_id = job.id
    failed = Mock(invoke=Mock(side_effect=AgentFailure("agent_invalid_result", retryable=True)))
    assert run_one(runtime, failed, job_id)
    detail = client.get(f"/api/english/records/{record['id']}").json()
    assert detail["items"][0]["feedbackStatus"] == "failed"
    assert detail["items"][0]["feedbackError"]
    assert detail["items"][0]["feedbackCanRetry"] is True
    retry = f"/api/english/records/{item['feedbackId']}/retry"
    assert client.post(retry, headers=_OTHER).status_code == 404
    assert client.post(retry).status_code == 202
    assert run_one(runtime, DemoAgent(), job_id)
    assert _evidence_count(runtime) == 0


def test_english_agent_blind_solving_and_subjective_feedback_are_separate():
    original = english_demo(_photo())
    a, b, _, writing = original.items
    secret = "STUDENT_ANSWER_DO_NOT_SOLVE_WITH_THIS"
    a = a.source.model_copy(update={"answer": secret})
    model = SimpleNamespace(infer=AsyncMock(side_effect=[
        (EnglishReadPage(quality="readable", items=[a, b.source, writing.source]), "reader"),
        (EnglishSolutions(solutions=[
            EnglishSolution(index=0, unambiguous=True, answers=["apple"], explanation="A fruit."),
            EnglishSolution(index=1, unambiguous=True, answers=["B"], explanation="She is."),
            EnglishSolution(index=1, unambiguous=True, answers=["A"], explanation="Conflicting."),
        ]), "solver"),
        (EnglishReviews(reviews=[EnglishReviewEntry(index=2, feedback=DEMO_FEEDBACK)]), "reviewer"),
    ]))
    result = asyncio.run(MathAgent(model, "test").invoke(_photo()))
    assert result.items[0].problem.answers == ["apple"]
    assert result.items[1].problem is None
    assert result.items[2].feedback == DEMO_FEEDBACK
    read, solve, review = model.infer.await_args_list
    assert read.kwargs["image"].startswith("data:image/jpeg;base64,")
    assert not solve.kwargs and not review.kwargs
    assert secret not in solve.args[2] and "answers" not in solve.args[2]
    assert [item["index"] for item in json.loads(solve.args[2])] == [0, 1]
    assert [item["index"] for item in json.loads(review.args[2])] == [2]


def test_english_generation_requires_independent_answer_consensus():
    request = _request()
    problems = demo_problems(request)
    model = SimpleNamespace(infer=AsyncMock(side_effect=[
        (EnglishCandidates(items=problems), "writer"),
        (EnglishSolutions(solutions=[
            EnglishSolution(index=i, unambiguous=True, answers=p.answers if i != 1 else ["wrong"],
                            explanation="Independent reference.")
            for i, p in enumerate(problems)
        ]), "solver"),
    ]))
    result = asyncio.run(MathAgent(model, "test").invoke(request))
    assert [p.answers for p in result.items] == [["apple"], ["school"]]
    solve = model.infer.await_args_list[1]
    assert not solve.kwargs
    assert all(set(item["prompt"]) == set(EnglishPrompt.model_fields) for item in json.loads(solve.args[2]))


@pytest.mark.parametrize("agent_request", [_photo(), _request(), ReviewEnglish(
    version="1", operation="review_english",
    prompt=demo_problems(_request("english-writing"))[0].model_dump(include=set(EnglishPrompt.model_fields)),
    answer="I like English.",
)])
def test_http_agent_supports_all_english_operations(tmp_path, agent_request):
    agent = HttpAgent(Settings(_env_file=None, mode="local", data_dir=tmp_path, agent_url="http://127.0.0.1:8080"), None)
    agent.client.close()
    agent.client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=english_demo(agent_request).model_dump())
    ))
    try:
        assert agent.invoke(agent_request) == english_demo(agent_request)
    finally:
        agent.close()


def test_english_migration_preserves_existing_math_rows(tmp_path):
    config = Config()
    config.set_main_option("script_location", str(Path("services/app/edu_app/migrations").resolve()))
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            config.attributes["connection"] = conn
            command.upgrade(config, "0002")
            conn.execute(text("""
                INSERT INTO submissions(id, owner, digest, created_at, revision)
                VALUES ('legacy', 'demo-a', :digest, 1, 1)
            """), {"digest": b"x" * 32})
            conn.execute(text("""
                INSERT INTO jobs(id, submission_id, status, available_at, attempts, manual_retries)
                VALUES ('legacy-job', 'legacy', 'done', 1, 1, 0)
            """))
            conn.execute(text("""
                INSERT INTO mastery_evidence(source_id, owner, skill, problem_key, score, occurred_at, submission_id)
                VALUES ('legacy-evidence', 'demo-a', 'rational-sum', 'legacy-problem', 2, 1, 'legacy')
            """))
            command.upgrade(config, "head")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
            assert conn.execute(text("SELECT submission_id, english_id, status FROM jobs")).one() == ("legacy", None, "done")
            assert conn.execute(text("SELECT score, verification FROM mastery_evidence")).one() == (2, "rules")
            assert conn.execute(text("SELECT revision, ai_mastery FROM submissions")).one() == (1, 0)
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
            conn.execute(text("UPDATE submissions SET ai_mastery = true WHERE id = 'legacy'"))
            with pytest.raises(RuntimeError, match="Preserve AI homework"):
                command.downgrade(config, "0005")
            conn.execute(text("UPDATE submissions SET ai_mastery = false WHERE id = 'legacy'"))
            conn.execute(text("""
                INSERT INTO english_records(id, owner, kind, document, created_at)
                VALUES ('english-record', 'demo-a', 'worksheet', '{}', 2)
            """))
            with pytest.raises(RuntimeError, match="Export and explicitly remove"):
                command.downgrade(config, "0002")
            assert conn.scalar(text("SELECT COUNT(*) FROM english_records")) == 1
    finally:
        engine.dispose()
