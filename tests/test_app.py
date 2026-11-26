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
from edu_domain.contracts import MAX_UPLOAD_BYTES, ProblemSpec
from edu_domain.images import normalize_page

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


def test_open_grading_persists_reference_and_knowledge_without_mastery(client, runtime, open_math_item):
    from edu_app.db import KnowledgePoint

    open_math_item = open_math_item.model_copy(update={"source": open_math_item.source.model_copy(update={
        "stem": "先化简再求值",
    })})
    submission = _upload(client, demo_page())
    agent = Mock(spec=DemoAgent)
    agent.invoke.return_value = demo_analysis().model_copy(update={"items": [open_math_item]})
    assert run_one(runtime, agent, submission["jobId"])
    path = f"/api/submissions/{submission['id']}"
    result = client.get(path).json()["report"]["items"][0]
    assert (result["outcome"], result["expected"], result["verification"]) == ("incorrect", "2", "ai_review")
    assert result["stem"] == open_math_item.source.question.text
    assert result["toolChecked"] and not result["eligibleForMastery"] and not result["canCorrect"]
    assert result["knowledgeTitles"] == ["整式化简与代入求值"]
    assert len(result["knowledgeIds"]) == 1
    assert result["stepFeedback"]
    assert client.get(path, headers=_OTHER_STUDENT).status_code == 404
    assert all(skill["sampleCount"] == 0 for skill in client.get("/api/mastery").json())
    with runtime.db.session() as session:
        assert session.get(KnowledgePoint, result["knowledgeIds"][0]).origin == "discovered"
        assert session.get(Evidence, result["id"]) is None
        stored = session.get(Report, (submission["id"], 1)).document["items"][0]
        assert stored["rules_version"] == "math-open-v1"
        assert stored["analysis"]["assessment"]["first_tool"]["value"] == "2"


def test_long_multipage_analysis_renews_lease_before_more_work(client, runtime, monkeypatch):
    from edu_app import jobs

    image = BytesIO()
    Image.new("RGB", (192, 192), "white").save(image, "PNG")
    submission = _upload(client, demo_page(), image.getvalue())
    clock = {"now": now_ms()}
    monkeypatch.setattr(jobs, "now_ms", lambda: clock["now"])

    def analyze(_request):
        clock["now"] += 650_000
        assert _claim(runtime, submission["jobId"]) is None
        return demo_analysis()

    agent = Mock(spec=DemoAgent)
    agent.invoke.side_effect = analyze
    assert run_one(runtime, agent, submission["jobId"])
    assert agent.invoke.call_count == 2
    assert client.get(f"/api/submissions/{submission['id']}").json()["status"] == "done"
    with runtime.db.session() as session:
        assert session.get(Job, submission["jobId"]).attempts == 1


def test_unconfirmed_ai_review_does_not_publish_reference_steps(client, runtime, open_math_item):
    assessment = open_math_item.assessment
    item = open_math_item.model_copy(update={"assessment": assessment.model_copy(update={
        "review": assessment.review.model_copy(update={"source_consistent": False}),
    })})
    submission = _upload(client, demo_page())
    agent = Mock(spec=DemoAgent)
    agent.invoke.return_value = demo_analysis().model_copy(update={"items": [item]})
    assert run_one(runtime, agent, submission["jobId"])
    reported = client.get(f"/api/submissions/{submission['id']}").json()["report"]["items"][0]
    assert reported["outcome"] == "undetermined" and reported["reason"] == "source_mismatch"
    assert reported["expected"] is None and reported["stepFeedback"] == []


def test_open_analysis_http_budget_does_not_extend_other_operations(tmp_path):
    import httpx

    from edu_app.agent_client import HttpAgent
    from edu_app.config import Settings
    from edu_domain.contracts import AnalyzePage

    settings = Settings(_env_file=None, mode="local", data_dir=tmp_path, agent_url="http://127.0.0.1:8080")
    agent = HttpAgent(settings, None)
    agent.client.close()
    budgets = []

    def respond(request):
        budgets.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=demo_analysis().model_dump())

    agent.client = httpx.Client(transport=httpx.MockTransport(respond), timeout=settings.agent_timeout_seconds)
    try:
        for version in ("2", "1"):
            agent.invoke(AnalyzePage(version=version, operation="analyze_submission", image_base64="aaaa"))
    finally:
        agent.close()
    assert budgets == [600, 125]


@pytest.mark.parametrize(("upload_path", "record_path"), [
    ("/api/submissions", "/api/submissions"),
    ("/api/english/worksheets", "/api/english/records"),
])
@pytest.mark.parametrize(("image_format", "extension", "content_type"), [
    ("JPEG", "jpg", "image/jpeg"),
    ("PNG", "png", "image/png"),
])
@pytest.mark.parametrize(("size", "normalized_size"), [
    ((192, 192), (192, 192)),
    ((4284, 5712), (1350, 1800)),
])
def test_upload_accepts_single_frame_jpeg_and_png_once(
    client, upload_path, record_path, image_format, extension, content_type, size, normalized_size,
):
    image = BytesIO()
    with Image.new("RGB", size, "white") as source:
        source.save(image, image_format)
    files = [("files", (f"page.{extension}", image.getvalue(), content_type))]

    response = client.post(upload_path, files=files)
    assert response.status_code == 202, response.text
    assert client.post(upload_path, files=files).json() == response.json()
    page = client.get(f"{record_path}/{response.json()['id']}/pages/1")
    assert page.status_code == 200
    assert page.headers["Content-Type"] == "image/jpeg"
    assert len(page.content) <= 500_000
    with Image.open(BytesIO(page.content)) as normalized:
        assert normalized.format == "JPEG"
        assert normalized.size == normalized_size


@pytest.mark.parametrize("upload_path", ["/api/submissions", "/api/english/worksheets"])
def test_upload_rejects_multiframe_png(client, upload_path):
    image = BytesIO()
    Image.new("RGB", (192, 192), "white").save(
        image, "PNG", save_all=True, append_images=[Image.new("RGB", (192, 192), "black")],
    )
    response = client.post(upload_path, files=_files(image.getvalue()))
    assert response.status_code == 422
    assert response.json()["detail"] == "请上传单帧 JPG 或 PNG 图片。"
    assert client.get("/api/submissions").json() == []
    assert client.get("/api/english/records").json() == []


@pytest.mark.parametrize("upload_path", ["/api/submissions", "/api/english/worksheets"])
@pytest.mark.parametrize(("size", "reason"), [
    ((119, 240), "宽高均需至少 120 像素"),
    ((8000, 8001), "超过 6400 万像素安全上限"),
])
def test_upload_rejects_unsafe_dimensions_with_specific_reason(client, upload_path, size, reason):
    image = BytesIO()
    with Image.new("L", size, 255) as source:
        source.save(image, "JPEG")
    response = client.post(upload_path, files=[("files", ("page.jpg", image.getvalue(), "image/jpeg"))])
    assert response.status_code == 422
    assert reason in response.json()["detail"]
    assert f"{size[0]}×{size[1]}" in response.json()["detail"]
    assert client.get("/api/submissions").json() == []
    assert client.get("/api/english/records").json() == []


@pytest.mark.parametrize("upload_path", ["/api/submissions", "/api/english/worksheets"])
def test_automatic_resize_does_not_bypass_upload_byte_limit(client, upload_path):
    response = client.post(upload_path, files=_files(b"x" * (MAX_UPLOAD_BYTES + 1)))
    assert response.status_code == 422
    assert "8 MB" in response.json()["detail"]
    assert client.get("/api/submissions").json() == []
    assert client.get("/api/english/records").json() == []


def test_large_jpeg_keeps_orientation_and_removes_exif():
    image = BytesIO()
    with Image.new("RGB", (4284, 5712), "white") as source:
        source.paste((220, 30, 30), (0, 0, 500, 500))
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "synthetic worksheet"
        source.save(image, "JPEG", exif=exif)
    with Image.open(BytesIO(normalize_page(image.getvalue()))) as normalized:
        assert normalized.size == (1800, 1350)
        assert not normalized.getexif()
        red, green, blue = normalized.getpixel((1790, 10))
        assert red > 200 and green < 60 and blue < 60


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_image_at_source_pixel_safety_limit_is_resized(image_format):
    image = BytesIO()
    with Image.new("L", (8000, 8000), 255) as source:
        source.save(image, image_format)
    with Image.open(BytesIO(normalize_page(image.getvalue()))) as normalized:
        assert normalized.size == (1800, 1800)


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
