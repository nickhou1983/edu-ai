from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from edu_agent.demo import DemoAgent, demo_analysis, demo_page, demo_practice
from edu_app.agent_client import AgentFailure
from edu_app.db import Evidence, Job, Report, new_id, now_ms
from edu_app.jobs import LEASE_MS, MAX_ATTEMPTS, _claim, _fail, run_one
from edu_app.queue import _dispatch
from edu_domain.contracts import ProblemSpec

_OTHER_STUDENT = {"X-Demo-Student": "demo-b"}


def _files(*images):
    return [("files", (f"page-{index}.png", image, "image/png")) for index, image in enumerate(images, 1)]


def _upload(client, *images):
    response = client.post("/api/submissions", files=_files(*images))
    assert response.status_code == 202, response.json()
    return response.json()


def _skill(client, skill="rational-sum"):
    return next(item for item in client.get("/api/mastery").json() if item["id"] == skill)


def _practice(client):
    response = client.post("/api/practice-sets", json={"skill": "rational-sum", "count": 3})
    assert response.status_code == 202, response.json()
    return response.json()


def test_upload_and_message_replay_grade_once_and_keep_images_private(client, runtime):
    assert client.get("/api/submissions", headers={"X-Demo-Student": ""}).status_code == 401
    submission = _upload(client, demo_page())
    assert _upload(client, demo_page()) == submission
    path = f"/api/submissions/{submission['id']}"
    assert client.get(path).json()["report"] is None

    agent = Mock(spec=DemoAgent, wraps=DemoAgent())
    with ThreadPoolExecutor(2) as threads:
        jobs = [threads.submit(run_one, runtime, agent, submission["jobId"]) for _ in range(2)]
        assert sorted(job.result() for job in jobs) == [False, True]
    agent.invoke.assert_called_once()
    assert not run_one(runtime, agent, submission["jobId"])

    detail = client.get(path).json()
    assert (detail["status"], detail["revision"]) == ("done", 1)
    assert [item["outcome"] for item in detail["report"]["items"]] == [
        "correct",
        "incorrect",
        "correct",
        "correct",
        "incorrect",
        "partial",
        "blank",
    ]
    page = client.get(f"{path}/pages/1")
    assert page.headers["Content-Type"] == "image/jpeg"
    assert page.headers["Cache-Control"] == "private, no-store"
    assert page.content.startswith(b"\xff\xd8\xff")
    for private in (path, f"{path}/pages/1"):
        assert client.get(private, headers=_OTHER_STUDENT).status_code == 404
    assert client.post(f"{path}/retry", headers=_OTHER_STUDENT).status_code == 404
    assert client.get("/api/submissions", headers=_OTHER_STUDENT).json() == []
    assert all(item["sampleCount"] == 0 for item in client.get("/api/mastery", headers=_OTHER_STUDENT).json())
    skill = _skill(client)
    assert (skill["sampleCount"], skill["percent"], skill["status"]) == (3, 66, "reinforce")
    assert {entry["submissionId"] for entry in skill["evidence"]} == {submission["id"]}


def test_transcription_revises_reference_grade_without_adding_mastery(client, runtime):
    submission = _upload(client, demo_page())
    assert run_one(runtime, DemoAgent())
    path = f"/api/submissions/{submission['id']}"
    item = client.get(path).json()["report"]["items"][1]
    correction = f"{path}/items/{item['id']}/transcription"
    assert client.patch(correction, json={"answer": " 2 ", "revision": 1}).json()["revision"] == 1
    assert (
        client.patch(correction, json={"answer": "8", "revision": 1}, headers=_OTHER_STUDENT).status_code
        == 404
    )

    response = client.patch(correction, json={"answer": "8", "revision": 1})
    assert response.status_code == 200
    corrected = response.json()
    assert corrected["revision"] == 2
    assert corrected["report"]["items"][1]["outcome"] == "correct"
    assert corrected["report"]["items"][1]["corrected"] is True
    assert client.patch(correction, json={"answer": "8", "revision": 2}).json() == corrected
    assert client.patch(correction, json={"answer": "7", "revision": 1}).status_code == 409
    assert _skill(client)["status"] == "insufficient"
    assert _skill(client)["sampleCount"] == 2
    with runtime.db.session() as db:
        assert db.get(Report, (submission["id"], 1)).document["items"][1]["outcome"] == "incorrect"
        assert db.get(Evidence, item["id"]) is None


def test_incomplete_upload_and_unclear_or_missing_page_do_not_add_evidence(client, runtime):
    image = BytesIO()
    Image.new("RGB", (192, 192), "white").save(image, "PNG")
    unclear = image.getvalue()
    assert client.post("/api/submissions", files=_files(demo_page(), b"not an image")).status_code == 422
    assert client.post("/api/submissions", files=_files(demo_page(), demo_page())).status_code == 422
    assert client.get("/api/submissions").json() == []

    submission = _upload(client, demo_page(), unclear)
    agent = Mock(spec=DemoAgent)
    data = demo_analysis()
    agent.invoke.side_effect = [data, data.model_copy(update={"quality": "unclear", "items": []})]
    assert run_one(runtime, agent)
    detail = client.get(f"/api/submissions/{submission['id']}").json()
    assert detail["pages"] == [1, 2]
    assert detail["report"]["retakePages"] == [2]
    assert {item["page"] for item in detail["report"]["items"]} == {1}

    missing = _upload(client, unclear)
    runtime.assets.delete(f"{missing['id']}/1.jpg")
    assert run_one(runtime, agent)
    detail = client.get(f"/api/submissions/{missing['id']}").json()
    assert detail["status"] == "failed"
    assert detail["report"] is None
    assert detail["canRetry"] is False
    assert agent.invoke.call_count == 2
    assert _skill(client)["sampleCount"] == 3


def test_transient_job_failure_recovers_with_one_manual_retry_and_a_new_lease(client, runtime, monkeypatch):
    submission = _upload(client, demo_page())
    job_id = submission["jobId"]
    detail_path = f"/api/submissions/{submission['id']}"
    agent = Mock(spec=DemoAgent)
    agent.invoke.side_effect = AgentFailure("agent_unavailable", retryable=True)
    clock = [now_ms() + 1_000]
    monkeypatch.setattr("edu_app.jobs.now_ms", lambda: clock[0])
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert run_one(runtime, agent, job_id)
        assert not run_one(runtime, agent, job_id)
        info = client.get(detail_path).json()
        assert info["status"] == ("queued" if attempt < MAX_ATTEMPTS else "failed")
        clock[0] += attempt * 3_000
    assert info["canRetry"] is True

    retry = client.post(f"{detail_path}/retry")
    assert retry.status_code == 202
    assert (retry.json()["jobId"], retry.json()["status"]) == (job_id, "queued")
    assert client.post(f"{detail_path}/retry").status_code == 409
    abandoned = _claim(runtime, job_id)
    assert abandoned is not None and not run_one(runtime, DemoAgent(), job_id)
    clock[0] += LEASE_MS + 1
    assert run_one(runtime, DemoAgent(), job_id)
    _fail(runtime, abandoned, AgentFailure("agent_unavailable", retryable=True))
    detail = client.get(detail_path).json()
    assert (detail["status"], detail["revision"], detail["canRetry"]) == ("done", 1, False)
    assert _skill(client)["sampleCount"] == 3


def test_outbox_send_failure_rolls_back_delivery_and_success_is_not_dispatched_twice(client, runtime):
    service_bus = pytest.importorskip("azure.servicebus.exceptions")
    submission = _upload(client, demo_page())
    sender = Mock()
    sender.send_messages.side_effect = service_bus.ServiceBusConnectionError(message="test outage")
    with pytest.raises(service_bus.ServiceBusConnectionError):
        _dispatch(runtime, sender)
    with runtime.db.session() as db:
        assert db.get(Job, submission["jobId"]).dispatched_at is None
    sender.send_messages.side_effect = None
    assert _dispatch(runtime, sender)
    assert _dispatch(runtime, sender)
    assert not _dispatch(runtime, sender)
    delivered = [str(call.args[0]) for call in sender.send_messages.call_args_list[1:]]
    with runtime.db.session() as db:
        expected = {job.id for job in db.query(Job).all()}
    assert set(delivered) == expected
    assert len(delivered) == len(expected) == 2


def test_practice_withholds_answers_and_uses_first_unhinted_encounter(client, runtime):
    practice = _practice(client)
    assert practice["purpose"] == "diagnose"
    assert run_one(runtime, DemoAgent())
    path = f"/api/practice-sets/{practice['id']}"
    detail = client.get(path).json()
    hinted, independent, _ = detail["items"]
    assert len({item["stem"] for item in detail["items"]}) == 3
    assert all(item["result"] is None and "expected" not in item for item in detail["items"])
    hinted_path = f"{path}/items/{hinted['id']}"
    independent_path = f"{path}/items/{independent['id']}"
    assert client.get(path, headers=_OTHER_STUDENT).status_code == 404
    assert client.post(f"{hinted_path}/hint", headers=_OTHER_STUDENT).status_code == 404
    assert (
        client.post(f"{independent_path}/answer", json={"answer": "-6"}, headers=_OTHER_STUDENT).status_code
        == 404
    )

    assert client.post(f"{hinted_path}/hint").json()["hint"]
    hinted_result = client.post(f"{hinted_path}/answer", json={"answer": "-5"}).json()
    assert hinted_result["correct"] is True
    assert hinted_result["eligibleForMastery"] is False
    assert _skill(client)["sampleCount"] == 0

    assert client.post(f"{independent_path}/answer", json={"answer": "??"}).status_code == 422
    submission = client.post(f"{independent_path}/answer", json={"answer": "-6"})
    assert submission.status_code == 200
    assert submission.json()["correct"] is True
    assert submission.json()["eligibleForMastery"] is True
    assert client.post(f"{independent_path}/answer", json={"answer": "-6"}).json() == submission.json()
    assert client.post(f"{independent_path}/answer", json={"answer": "-4"}).status_code == 409
    assert client.get(path).json()["items"][2]["result"] is None

    repeated = _practice(client)
    assert run_one(runtime, DemoAgent())
    repeated_path = f"/api/practice-sets/{repeated['id']}"
    repeated_item = client.get(repeated_path).json()["items"][1]
    path_answer = f"{repeated_path}/items/{repeated_item['id']}/answer"
    assert client.post(path_answer, json={"answer": "0"}).json()["correct"] is False
    evidence = _skill(client)["evidence"]
    assert [(item["itemId"], item["score"]) for item in evidence] == [(independent["id"], 2)]


def test_generation_does_not_publish_a_partially_verified_group(client, runtime):
    practice = _practice(client)

    def candidates(request):
        invalid_group = [
            ProblemSpec(task="evaluate", expression="-7+2", choices=[]),
            ProblemSpec(task="evaluate", expression="(-7)+2", choices=[]),
            ProblemSpec(task="evaluate", expression="2/0", choices=[]),
            ProblemSpec(task="evaluate", expression="2*3", choices=[]),
            ProblemSpec(task="evaluate", expression="-8+2+1", choices=[]),
        ]
        return demo_practice(request).model_copy(update={"items": invalid_group})

    agent = Mock(spec=DemoAgent)
    agent.invoke.side_effect = candidates
    assert run_one(runtime, agent)
    result = client.get(f"/api/practice-sets/{practice['id']}").json()
    assert (result["status"], result["items"]) == ("failed", [])
    assert agent.invoke.call_count == 3
    assert "通过验证" in result["error"]


def test_mastery_uses_latest_ten_distinct_problems_and_exact_thresholds(client, runtime):
    cases = [
        ("rational-sum", [2] * 5 + [1] + [0] * 4, 55, "priority"),
        ("rational-product", [2] * 6 + [0] * 4, 60, "reinforce"),
        ("rational-mixed", [2] * 7 + [1] + [0] * 2, 75, "reinforce"),
        ("like-terms", [2] * 8 + [0] * 2, 80, "stable"),
    ]
    with runtime.db.transaction() as session:
        for skill, scores, _, _ in cases:
            for index, score in enumerate([2, 2, *scores]):
                session.add(
                    Evidence(
                        source_id=new_id(),
                        owner="demo-a",
                        skill=skill,
                        problem_key=f"p{index}",
                        score=score,
                        occurred_at=100 + index,
                    )
                )
            session.add(
                Evidence(
                    source_id=new_id(),
                    owner="demo-a",
                    skill=skill,
                    problem_key="p2",
                    score=0,
                    occurred_at=200,
                )
            )
    for skill, _, percent, status in cases:
        stats = _skill(client, skill)
        assert (stats["sampleCount"], stats["percent"], stats["status"]) == (10, percent, status)
        assert [evidence["occurredAt"] for evidence in stats["evidence"]] == list(range(111, 101, -1))
