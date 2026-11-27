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
    EnglishAnalyzedItem,
    EnglishAssessment,
    EnglishCandidates,
    EnglishFeedback,
    EnglishGradeResult,
    EnglishObjectiveReview,
    EnglishPrompt,
    EnglishReadPage,
    EnglishReviewEntry,
    EnglishReviews,
    EnglishSolution,
    EnglishSolutions,
    GenerateEnglish,
    GradeEnglish,
    ReviewEnglish,
)
from edu_domain.english import ENGLISH_SKILLS, english_correct, english_outcome, english_prompt, grade_english
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


def _assessment(problem, *, index=0, outcome="correct", **review_fields):
    review = EnglishObjectiveReview.model_validate({
        "index": index, "source_consistent": True, "references_agree": True, "outcome": outcome,
        "explanation": "题面和作答均已核对。", "partial_basis": None,
        "mastery_skill": problem.skill, "mastery_basis": "直接核对所要求的词形。",
        **review_fields,
    })
    return EnglishAssessment(first=problem, second=problem.model_copy(), review=review)


@pytest.mark.parametrize(("case", "outcome", "score"), [
    ("partial", "partial", 1), ("unsupported_partial", "undetermined", None),
    ("conflicting_solvers", "undetermined", None), ("source_mismatch", "undetermined", None),
    ("blank", "blank", None), ("missing_attribution", "correct", None),
    ("choice_partial", "undetermined", None), ("alternate_reading", "correct", 2),
])
def test_english_ai_checks_grade_and_mastery_independently(case, outcome, score):
    skill = {"choice_partial": "english-grammar", "alternate_reading": "english-reading"}.get(
        case, "english-vocabulary",
    )
    problem = demo_problems(_request(skill))[0]
    answer, mark = problem.answers[0], "present"
    if "partial" in case:
        if case == "choice_partial":
            answer = "B"
        else:
            problem = problem.model_copy(update={"instruction": "Write both fruit names.", "answers": ["apple and pear"]})
            answer = "apple"
        assessment = _assessment(problem, outcome="partial",
                                 partial_basis=None if case == "unsupported_partial" else "写对了 apple。")
    elif case == "blank":
        answer, mark = None, "blank"
        assessment = _assessment(problem, outcome="blank")
    elif case == "missing_attribution":
        assessment = _assessment(problem, mastery_basis=" ")
    elif case == "source_mismatch":
        assessment = _assessment(problem, source_consistent=False)
    elif case == "conflicting_solvers":
        assessment = _assessment(problem).model_copy(update={
            "second": problem.model_copy(update={"answers": ["pear"]}),
        })
    elif case == "alternate_reading":
        assert problem.answers == ["two"]
        problem = problem.model_copy(update={"instruction": "Give the number of books in words or digits."})
        answer = "2"
        assessment = _assessment(problem)
    grade = grade_english(english_prompt(problem), answer, mark, assessment, index=0)
    assert (grade.outcome, grade.score) == (outcome, score)


def test_english_ai_worksheet_resumes_completed_pages_and_retries_only_pending_items(client, runtime):
    runtime.settings.english_ai_mastery_enabled = True
    with BytesIO() as output:
        Image.new("RGB", (192, 192), "white").save(output, "PNG")
        page_two = output.getvalue()
    files = [
        ("files", ("one.png", english_demo_page(), "image/png")),
        ("files", ("two.png", page_two, "image/png")),
    ]
    response = client.post("/api/english/worksheets", files=files)
    record = response.json()
    assert response.status_code == 202
    path = f"/api/english/records/{record['id']}"
    legacy = english_demo(_photo())
    apple, grammar, reading, writing = legacy.items

    def page(*items):
        return legacy.model_copy(update={"version": "2", "items": list(items)})

    def pending(analyzed):
        return EnglishAnalyzedItem(source=analyzed.source, problem=None, feedback=None, review_timed_out=True)

    def reviewed(analyzed, outcome="correct"):
        return EnglishAnalyzedItem(source=analyzed.source, problem=None, feedback=None,
                                   assessment=_assessment(analyzed.problem, outcome=outcome))

    def timeout():
        return AgentFailure("agent_timeout", retryable=True)

    with runtime.db.transaction() as session:
        session.get_one(Job, record["jobId"]).attempts = 2
    agent = Mock(invoke=Mock(side_effect=[page(reviewed(apple), pending(grammar)), timeout()]))
    assert run_one(runtime, agent, record["jobId"])
    assert [call.args[0].version for call in agent.invoke.call_args_list] == ["2", "2"]
    second_image = agent.invoke.call_args.args[0].image_base64
    assert client.get(path).json()["items"] == []
    assert _evidence_count(runtime) == 0

    runtime.settings.english_ai_mastery_enabled = False
    assert client.post("/api/english/worksheets", files=files).json()["id"] == record["id"]
    assert client.post(f"{path}/retry").status_code == 202
    agent.invoke.reset_mock(side_effect=True)
    agent.invoke.return_value = page(pending(reading), writing)
    assert run_one(runtime, agent, record["jobId"])
    agent.invoke.assert_called_once()
    new_request = agent.invoke.call_args.args[0]
    assert (new_request.version, new_request.image_base64, new_request.review_items) == ("2", second_image, None)
    original = client.get(path).json()
    assert original["canRetryReview"]
    assert [item["outcome"] for item in original["items"]] == ["correct", "undetermined", "undetermined", "feedback"]
    assert all("reviewSource" not in item for item in original["items"])

    assert client.post(f"{path}/retry", headers=_OTHER).status_code == 404
    assert client.post(f"{path}/retry").status_code == 202
    assert client.post(f"{path}/retry").status_code == 409
    agent.invoke.reset_mock()
    agent.invoke.side_effect = [page(reviewed(grammar, "incorrect")), timeout()]
    assert run_one(runtime, agent, record["jobId"])
    assert [call.args[0].review_items for call in agent.invoke.call_args_list] == [[grammar.source], [reading.source]]
    during = client.get(path).json()
    assert during["items"][0] == original["items"][0]
    assert during["items"][1]["outcome"] == "incorrect"
    assert during["items"][2]["reason"] == "review_timeout"
    assert _evidence_count(runtime) == 2

    with runtime.db.transaction() as session:
        session.get_one(Job, record["jobId"]).available_at = 0
    agent.invoke.reset_mock(side_effect=True)
    agent.invoke.return_value = page(reviewed(reading))
    assert run_one(runtime, agent, record["jobId"])
    agent.invoke.assert_called_once()
    assert agent.invoke.call_args.args[0].review_items == [reading.source]
    result = client.get(path).json()
    assert result["status"] == "done" and not result["canRetryReview"]
    assert [item["id"] for item in result["items"]] == [item["id"] for item in original["items"]]
    assert [item["outcome"] for item in result["items"]] == ["correct", "incorrect", "correct", "feedback"]
    assert not run_one(runtime, agent, record["jobId"])
    with runtime.db.session() as session:
        evidence = session.scalars(select(Evidence)).all()
        assert len(evidence) == 3
        assert {e.occurred_at for e in evidence} == {record["createdAt"]}
        assert {e.verification for e in evidence} == {"ai_review"}


def test_english_reanalysis_discards_old_checkpoints_and_replaces_worksheet_evidence(client, runtime):
    runtime.settings.english_ai_mastery_enabled = True
    with BytesIO() as output:
        Image.new("RGB", (192, 192), "white").save(output, "PNG")
        second_photo = output.getvalue()
    files = [
        ("files", ("one.png", english_demo_page(), "image/png")),
        ("files", ("two.png", second_photo, "image/png")),
    ]
    response = client.post("/api/english/worksheets", files=files)
    assert response.status_code == 202
    created = response.json()
    path = f"/api/english/records/{created['id']}"
    action = f"{path}/reanalyze"
    job_id = created["jobId"]
    assert client.post(action, headers=_OTHER).status_code == 404
    assert client.post(action).status_code == 409
    legacy = english_demo(_photo())
    apple, grammar, _, writing = legacy.items
    correct = EnglishAnalyzedItem(
        source=apple.source, problem=None, feedback=None, assessment=_assessment(apple.problem),
    )
    pending = EnglishAnalyzedItem(source=grammar.source, problem=None, feedback=None, review_timed_out=True)
    initial = legacy.model_copy(update={"version": "2", "items": [correct, pending]})
    second_result = initial.model_copy(update={"items": [writing]})
    agent = Mock(invoke=Mock(side_effect=[initial, second_result]))
    assert run_one(runtime, agent, job_id)
    original = client.get(path).json()
    assert original["canReanalyze"] and original["canRetryReview"]
    assert original["items"][0]["outcome"] == "correct" and _evidence_count(runtime) == 1

    assert client.post(f"{path}/retry").status_code == 202
    agent.invoke.side_effect = AgentFailure("agent_no_result")
    assert run_one(runtime, agent, job_id)
    assert agent.invoke.call_args.args[0].review_items == [grammar.source]
    runtime.settings.english_ai_mastery_enabled = False
    practice = _practice(client, runtime)
    assert client.post(f"/api/english/records/{practice['id']}/reanalyze").status_code == 409
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: client.post(action).status_code, range(2))) == [202, 409]
    reuse = client.post("/api/english/worksheets", files=files).json()
    assert (reuse["id"], reuse["jobId"], reuse["isReanalyzing"]) == (created["id"], job_id, True)
    assert client.get(path).json()["items"] == original["items"]
    agent.invoke.reset_mock()
    agent.invoke.side_effect = [
        initial.model_copy(update={"items": [pending]}), second_result.model_copy(update={"quality": "unclear"}),
    ]
    assert run_one(runtime, agent, job_id)
    attempts = [call.args[0] for call in agent.invoke.call_args_list]
    assert [r.image_base64 for r in attempts] == [
        _photo().image_base64, b64encode(normalize_page(second_photo)).decode("ascii"),
    ]
    assert all(r.version == "2" and r.review_items is None for r in attempts)
    failed = client.get(path).json()
    assert failed["status"] == "failed" and failed["canReanalyze"] and not failed["canRetry"]
    assert "已保留原报告" in failed["error"] and "超时" in failed["error"]
    assert failed["items"] == original["items"] and _evidence_count(runtime) == 1

    assert client.post(action).status_code == 202
    incorrect = correct.model_copy(update={
        "source": apple.source.model_copy(update={"answer": "pear"}),
        "assessment": _assessment(apple.problem, outcome="incorrect"),
    })
    agent.invoke.reset_mock()
    agent.invoke.side_effect = [
        initial.model_copy(update={"items": [incorrect]}), AgentFailure("agent_timeout", retryable=True),
    ]
    assert run_one(runtime, agent, job_id)
    assert [call.args[0] for call in agent.invoke.call_args_list] == attempts
    checkpointed = client.get(path).json()
    assert checkpointed["status"] == "queued" and checkpointed["isReanalyzing"]
    assert checkpointed["items"] == original["items"] and _evidence_count(runtime) == 1
    with runtime.db.transaction() as session:
        session.get_one(Job, job_id).available_at = 0
    agent.invoke.reset_mock(side_effect=True)
    agent.invoke.return_value = second_result
    assert run_one(runtime, agent, job_id)
    agent.invoke.assert_called_once_with(attempts[1])
    updated = client.get(path).json()
    assert updated["status"] == "done" and not updated["isReanalyzing"] and updated["canReanalyze"]
    assert [item["outcome"] for item in updated["items"]] == ["incorrect", "feedback"]
    with runtime.db.session() as session:
        evidence = session.scalars(select(Evidence)).one()
        assert (evidence.source_id, evidence.english_id, evidence.score, evidence.occurred_at) == (
            updated["items"][0]["id"], created["id"], 0, created["createdAt"],
        )


def test_english_ai_practice_uses_durable_grading_and_deduplicates_legacy_answers(client, runtime):
    old = _upload(client)
    assert run_one(runtime, DemoAgent(), old["jobId"])
    old_practice = _practice(client, runtime)
    runtime.settings.english_ai_mastery_enabled = True
    skipped = old_practice["items"][0]
    # A practice created before the switch keeps synchronous matching.
    legacy_answer = client.post(f"/api/english/records/{old_practice['id']}/items/{skipped['id']}/answer",
                                json={"answer": "apple"}).json()
    assert legacy_answer["verification"] == "legacy_match" and "assessmentStatus" not in legacy_answer

    generated = english_demo(_request().model_copy(update={"count": 4}))
    partial = generated.items[1].model_copy(update={
        "instruction": "Write both fruit names.", "answers": ["apple and pear"],
    })
    generated.items[1] = partial
    created = client.post("/api/english/practices", json={"skill": "english-vocabulary", "count": 4}).json()
    assert run_one(runtime, Mock(invoke=Mock(return_value=generated)), created["jobId"])
    path = f"/api/english/records/{created['id']}"
    items = client.get(path).json()["items"]
    after_upload = client.get("/api/english/skills").json()[0]
    assert (after_upload["sampleCount"], after_upload["percent"]) == (1, None)
    runtime.settings.english_ai_mastery_enabled = False

    answers = ("pear", "apple", "school", "friend")
    for i, (item, problem, answer) in enumerate(zip(items, generated.items, answers, strict=True)):
        submit_path = f"{path}/items/{item['id']}/answer"
        assert item["expected"] is None
        accepted = client.post(submit_path, json={"answer": answer}).json()
        assert accepted["assessmentStatus"] == "queued" and accepted["outcome"] is None
        assert accepted["expected"] is None and accepted["explanation"] is None
        assert client.get(path).json()["items"][i] == accepted
        assert client.post(submit_path, json={"answer": answer}).json() == accepted
        assert client.post(submit_path, json={"answer": "different"}).status_code == 409
        with runtime.db.session() as session:
            assessment_job = session.scalar(select(Job).where(Job.english_id == accepted["assessmentId"])).id
            submitted_at = session.get_one(EnglishRecord, accepted["assessmentId"]).created_at
        assert client.post(f"/api/english/records/{accepted['assessmentId']}/retry", headers=_OTHER).status_code == 404
        assessment = _assessment(problem, outcome=["incorrect", "partial", "correct", "correct"][i],
                                 partial_basis="写对了 apple。" if i == 1 else None)
        if i == 3:
            with runtime.db.transaction() as session:
                session.get_one(Job, assessment_job).attempts = 2
            unavailable = Mock(invoke=Mock(side_effect=AgentFailure("agent_timeout", retryable=True)))
            assert run_one(runtime, unavailable, assessment_job)
            failed = client.get(path).json()["items"][i]
            assert failed["assessmentStatus"] == "failed" and failed["assessmentCanRetry"]
            assert failed["expected"] is None
            assert client.post(f"/api/english/records/{accepted['assessmentId']}/retry").status_code == 202
            wrong = problem.model_copy(update={"answers": ["friends"]})
            assessment = assessment.model_copy(update={"first": wrong, "second": wrong})
        agent = Mock(invoke=Mock(return_value=EnglishGradeResult(
            version="2", operation="grade_english", provenance=generated.provenance, assessment=assessment,
        )))
        assert run_one(runtime, agent, assessment_job)
        agent.invoke.assert_called_once_with(GradeEnglish(
            version="2", operation="grade_english", prompt=english_prompt(problem), answer=answer,
        ))
        assert not run_one(runtime, agent, assessment_job)
        result = client.get(path).json()["items"][i]
        assert result["assessmentStatus"] == "done"
        if i == 3:
            assert (result["outcome"], result["reason"], result["expected"]) == (
                "undetermined", "model_disagreement", None,
            )
        else:
            assert result["verification"] == "ai_review"
            with runtime.db.session() as session:
                evidence = session.get_one(Evidence, item["id"])
                assert (evidence.english_id, evidence.occurred_at) == (created["id"], submitted_at)
                assert evidence.score == [0, 1, 2][i]

    mastery = client.get("/api/english/skills").json()[0]
    assert (mastery["sampleCount"], mastery["percent"]) == (3, 83)
    assert {e["verification"] for e in mastery["evidence"]} == {"legacy_match", "ai_review"}
    assert {e["englishId"] for e in mastery["evidence"]} == {old["id"], created["id"]}
    assert all(s["sampleCount"] == 0 for s in client.get("/api/english/skills", headers=_OTHER).json())
    assert all(s["sampleCount"] == 0 for s in client.get("/api/mastery").json())
    assert {record["kind"] for record in client.get("/api/english/records").json()} == {"worksheet", "practice"}


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
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
            assert conn.execute(text("SELECT submission_id, english_id, status, reanalysis FROM jobs")).one() == (
                "legacy", None, "done", 0,
            )
            assert conn.execute(text("SELECT score, verification FROM mastery_evidence")).one() == (2, "rules")
            assert conn.execute(text("SELECT revision, ai_mastery FROM submissions")).one() == (1, 0)
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
            conn.execute(text("UPDATE jobs SET status = 'queued', reanalysis = true"))
            with pytest.raises(RuntimeError, match="Finish queued or running"):
                command.downgrade(config, "0006")
            conn.execute(text("UPDATE jobs SET status = 'done', reanalysis = false"))
            conn.execute(text("UPDATE submissions SET ai_mastery = true WHERE id = 'legacy'"))
            with pytest.raises(RuntimeError, match="Preserve AI homework"):
                command.downgrade(config, "0005")
            conn.execute(text("UPDATE submissions SET ai_mastery = false WHERE id = 'legacy'"))
            conn.execute(text("""
                INSERT INTO english_records(id, owner, kind, document, created_at)
                VALUES ('english-record', 'demo-a', 'worksheet', '{}', 2)
            """))
            conn.execute(text("UPDATE english_records SET document = :doc"), {"doc": '{"aiMastery":true}'})
            with pytest.raises(RuntimeError, match="Preserve AI homework"):
                command.downgrade(config, "0005")
            conn.execute(text("UPDATE english_records SET document = '{}'"))
            with pytest.raises(RuntimeError, match="Export and explicitly remove"):
                command.downgrade(config, "0002")
            assert conn.scalar(text("SELECT COUNT(*) FROM english_records")) == 1
    finally:
        engine.dispose()
