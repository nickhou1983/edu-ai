import asyncio
import json
import shutil
import socket
import subprocess
from base64 import b64decode
from contextlib import asynccontextmanager, nullcontext
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from edu_agent import knowledge_evaluation as evaluation
from edu_domain.contracts import MAX_PAGE_BYTES, DiscoverKnowledge, KnowledgeDiscoveryResult

PROVENANCE = {"model": "synthetic-model", "prompt": "knowledge-reference-v1", "agent": "synthetic-agent"}


def _copy_suite(tmp_path):
    shutil.copytree(evaluation.ROOT / ".foundry", tmp_path / ".foundry", ignore=shutil.ignore_patterns("results"))
    return evaluation.read_cases(tmp_path)


def _discovery(row):
    return KnowledgeDiscoveryResult(
        version="1", operation="discover_knowledge", provenance=PROVENANCE,
        quality=row["expected"]["quality"],
        items=[{
            "label": item["label"],
            "region": {
                "left": 100, "right": 900, "top": item["region"][1] + 10, "bottom": item["region"][3] - 10,
            },
            "stem": "Private synthetic printed question",
            "points": [{
                "existing_id": identifier, "title": "General concept", "unit": "Synthetic unit",
                "description": "Reusable instructional concept",
            } for identifier in item["existing_ids"]],
            "feedback": {
                "strengths": ["A useful intermediate step."], "improvements": ["Revisit the transformation."],
                "suggested_response": "An unverified possible response.",
            } if item["feedback"] else None,
        } for item in row["expected"]["items"]],
    )


def _assessment(rubric, state="satisfied"):
    return evaluation.Assessment(
        result="pass", reason="Synthetic judge response",
        checks=[
            {"id": criterion["id"], "result": state, "reason": "Synthetic criterion evidence"}
            for criterion in rubric["criteria"]
        ],
    )


def _fake_live(monkeypatch, tmp_path, manifest):
    projects = pytest.importorskip("azure.ai.projects")
    identity = pytest.importorskip("azure.identity")
    agent = SimpleNamespace(
        state="enabled", versions=SimpleNamespace(latest=SimpleNamespace(version=manifest["agentVersion"])),
    )
    version = SimpleNamespace(status="active", metadata={"image": "example/image@" + manifest["agentImageDigest"]})
    project = SimpleNamespace(agents=SimpleNamespace(
        get=Mock(return_value=agent), get_version=Mock(return_value=version),
    ))
    monkeypatch.setattr(projects, "AIProjectClient", lambda **_kwargs: nullcontext(project))
    monkeypatch.setattr(identity, "AzureCliCredential", lambda: nullcontext(object()))
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    return agent, version


def test_offline_preparation_validates_real_images_without_credentials_or_network(monkeypatch, tmp_path, capsys):
    manifest, _ = _copy_suite(tmp_path)
    read_cases = evaluation.read_cases
    denied = Mock(side_effect=AssertionError("Offline actions must not use network or Azure CLI"))
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    for action in ("prepare", "validate"):
        monkeypatch.setattr(
            evaluation, "read_cases",
            Mock(wraps=lambda *, prepare: read_cases(tmp_path, prepare=prepare)),
        )
        assert evaluation.main([action]) == 0
    _, cases = read_cases(tmp_path)
    photos = [request for _, request in cases if isinstance(request, DiscoverKnowledge)]
    assert len(photos) == 6 and len(cases) == 15
    for request in photos:
        image = b64decode(request.image_base64, validate=True)
        assert len(image) <= MAX_PAGE_BYTES
        assert Image.open(BytesIO(image)).format == "JPEG"
    assert manifest["evaluationRunStarted"] is False
    assert '"networkCalls": 0' in capsys.readouterr().out
    denied.assert_not_called()


def test_live_approval_is_checked_before_reading_data_or_acquiring_credentials(monkeypatch):
    denied = Mock(side_effect=AssertionError("Approval must be checked first"))
    monkeypatch.setattr(evaluation, "read_cases", denied)
    for args in (["run"], ["prepare", "--with-judge"], ["validate", "--approve-live"]):
        with pytest.raises(SystemExit) as exc:
            evaluation.main(args)
        assert exc.value.code == 2
    denied.assert_not_called()


def test_dataset_lineage_math_references_and_image_limits_are_enforced(tmp_path):
    manifest, _ = _copy_suite(tmp_path)
    data = tmp_path / manifest["datasetFile"]
    original = data.read_text()
    rows = [json.loads(line) for line in original.splitlines()]
    rows[11]["ground_truth"]["answer"] = "4x^2-4x-3"
    data.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="Independent math"):
        evaluation.read_cases(tmp_path)
    rows[11]["ground_truth"]["answer"] = "9-4x+4x^2"
    data.write_text("\n".join(json.dumps(row) for row in rows))
    evaluation.read_cases(tmp_path)
    rows[-1]["sourceScenario"] = 14
    data.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="Every generated source"):
        evaluation.read_cases(tmp_path)
    data.write_text(original)
    image = tmp_path / rows[0]["fixture"]["file"]
    image.write_bytes(b"\xff\xd8\xff" + b"0" * MAX_PAGE_BYTES)
    with pytest.raises(ValueError, match="at most 500000"):
        evaluation.read_cases(tmp_path)
    rubric_file = tmp_path / manifest["rubricFile"]
    rubric = json.loads(rubric_file.read_text())
    rubric["criteria"][0]["operations"] = []
    rubric_file.write_text(json.dumps(rubric))
    with pytest.raises(ValueError, match="operation-scoped rubric"):
        evaluation.read_cases(tmp_path)


def test_artifacts_cannot_escape_the_selected_agent_root_or_silently_overwrite(tmp_path):
    _, cases = _copy_suite(tmp_path)
    with pytest.raises(ValueError, match="inside"):
        evaluation.local_path(tmp_path, "../outside.jpg")
    fixture = tmp_path / cases[0][0]["fixture"]["file"]
    fixture.write_bytes(b"different-user-content")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        evaluation.read_cases(tmp_path, prepare=True)
    assert fixture.read_bytes() == b"different-user-content"


def test_invocation_posts_unwrapped_typed_json_with_actual_jpeg_and_preserves_catalog():
    manifest, cases = evaluation.read_cases()
    row, request = cases[1]
    original = request.model_dump(mode="json")
    credential = SimpleNamespace(get_token=Mock(return_value=SimpleNamespace(token="synthetic-token")))

    def handler(http_request):
        assert http_request.url.params["api-version"] == "v1"
        assert http_request.headers["Authorization"] == "Bearer synthetic-token"
        payload = json.loads(http_request.content)
        assert payload == original and "input" not in payload and "messages" not in payload
        assert b64decode(payload["image_base64"]).startswith(b"\xff\xd8\xff")
        return httpx.Response(200, json=_discovery(row).model_dump(mode="json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = evaluation.invoke(client, manifest["invocationsEndpoint"], credential, request)
    credential.get_token.assert_called_once_with("https://ai.azure.com/.default")
    assert result.items[0].points[0].existing_id == "eng-g7-014"
    assert request.model_dump(mode="json") == original


@pytest.mark.parametrize("status,payload,error", [(503, {}, RuntimeError), (200, {"unexpected": True}, ValidationError)])
def test_invocation_surfaces_http_and_contract_failures_without_retry(status, payload, error):
    manifest, cases = evaluation.read_cases()
    handler = Mock(return_value=httpx.Response(status, json=payload))
    credential = SimpleNamespace(get_token=Mock(return_value=SimpleNamespace(token="synthetic-token")))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(error):
        evaluation.invoke(client, manifest["invocationsEndpoint"], credential, cases[0][1])
    assert handler.call_count == 1


def test_structural_checks_catch_forged_ids_shared_canaries_and_duplicates():
    _, cases = evaluation.read_cases()
    row, request = cases[2]
    result = _discovery(row)
    assert all(check["result"] == "satisfied" for check in evaluation.structural_checks(row, request, result))
    result.items[0].points[0].existing_id = "forged-id"
    result.items[0].points[0].description = "SYNTH-PUPIL-Q"
    checks = {check["id"]: check["result"] for check in evaluation.structural_checks(row, request, result)}
    assert checks["no_forged_ids"] == checks["no_shared_canaries"] == "violated"
    row, request = cases[6]
    result = evaluation.KnowledgePracticeResult(
        version="1", operation="generate_knowledge", provenance=PROVENANCE,
        items=[{
            "instruction": "Calculate 2 + 3.", "context": "", "reference_answer": "5",
            "explanation": "Unverified reference.",
        }] * 3,
    )
    checks = {check["id"]: check["result"] for check in evaluation.structural_checks(row, request, result)}
    assert checks["no_identical_exercises"] == "violated"
    request.count = 4
    checks = {check["id"]: check["result"] for check in evaluation.structural_checks(row, request, result)}
    assert checks["requested_count"] == "violated"


@pytest.mark.parametrize("state,expected", [
    ("satisfied", "pass"), ("violated", "fail"),
    ("not_assessable", "inconclusive"), ("not_applicable", "inconclusive"),
])
def test_judge_cannot_skip_required_criteria_or_turn_uncertainty_into_pass(state, expected):
    manifest, _ = evaluation.read_cases()
    rubric = json.loads((evaluation.ROOT / manifest["rubricFile"]).read_text())
    assessment = _assessment(rubric, state)
    assert evaluation.assess_criteria(assessment, rubric, "review_knowledge").result == expected
    for checks in (assessment.checks[:-1], [*assessment.checks, assessment.checks[0]]):
        assessment.checks = checks
        with pytest.raises(ValueError, match="omitted or duplicated"):
            evaluation.assess_criteria(assessment, rubric, "review_knowledge")


def test_optional_judge_uses_actual_image_strict_schema_and_no_content_storage(monkeypatch):
    pytest.importorskip("azure.ai.agentserver.invocations")
    import azure.ai.projects.aio as projects
    import azure.identity.aio as identity

    manifest, cases = evaluation.read_cases()
    rubric = json.loads((evaluation.ROOT / manifest["rubricFile"]).read_text())
    row, request = cases[0]
    create = AsyncMock(return_value=SimpleNamespace(
        status="completed", model="synthetic-model", output_text=_assessment(rubric).model_dump_json(),
    ))

    @asynccontextmanager
    async def project(**_kwargs):
        @asynccontextmanager
        async def openai(**options):
            assert options["max_retries"] == 0
            yield SimpleNamespace(responses=SimpleNamespace(create=create))

        yield SimpleNamespace(get_openai_client=openai)

    @asynccontextmanager
    async def credential():
        yield object()

    monkeypatch.setattr(projects, "AIProjectClient", project)
    monkeypatch.setattr(identity, "AzureCliCredential", credential)
    assert asyncio.run(evaluation.judge(manifest, row, request, _discovery(row))).result == "pass"
    kwargs = create.await_args.kwargs
    assert kwargs["store"] is False and kwargs["text"]["format"]["strict"] is True
    assert kwargs["text"]["format"]["schema"]["additionalProperties"] is False
    contents = kwargs["input"][0]["content"]
    assert contents[1]["image_url"] == "data:image/jpeg;base64," + request.image_base64
    payload = json.loads(contents[0]["text"])
    assert payload["ground_truth"] == row["ground_truth"]
    assert "fixture" not in payload and "image_base64" not in payload["request"]


def test_runner_persists_unassessed_quality_without_judge_and_records_partial_failure(monkeypatch, tmp_path):
    manifest, cases = evaluation.read_cases()
    row, request = cases[0]
    _fake_live(monkeypatch, tmp_path, manifest)
    invoke = Mock(return_value=_discovery(row))
    monkeypatch.setattr(evaluation, "invoke", invoke)
    assert evaluation.run(manifest, [(row, request)], with_judge=False) == 0
    summary_file = next((tmp_path / manifest["resultsDirectory"]).glob("*/summary.json"))
    summary = json.loads(summary_file.read_text())
    assert summary["qualityStatus"] == "not_assessed" and summary["completedCases"] == 1
    assert summary["status"] == "completed" and not summary["judgeRun"]
    assert "image_base64" not in (tmp_path / summary["itemsFile"]).read_text()
    invoke.side_effect = [evaluation.invoke.return_value, RuntimeError("Synthetic HTTP failure")]
    with pytest.raises(RuntimeError, match="Synthetic HTTP failure"):
        evaluation.run(manifest, [(row, request), (row, request)], with_judge=False)
    summaries = [
        json.loads(path.read_text())
        for path in (tmp_path / manifest["resultsDirectory"]).glob("*/summary.json")
    ]
    failed = next(summary for summary in summaries if summary["status"] == "failed")
    assert failed["completedCases"] == 1 and failed["qualityStatus"] == "not_assessed"
    assert failed["errorType"] == "RuntimeError"


def test_runner_refuses_changed_deployment_before_any_billable_invocation(monkeypatch, tmp_path):
    manifest, cases = evaluation.read_cases()
    agent, _ = _fake_live(monkeypatch, tmp_path, manifest)
    agent.versions.latest.version = "999"
    invoke = Mock(side_effect=AssertionError("Do not invoke a changed deployment"))
    monkeypatch.setattr(evaluation, "invoke", invoke)
    with pytest.raises(ValueError, match="Deployed Agent changed"):
        evaluation.run(manifest, cases, with_judge=False)
    invoke.assert_not_called()


def test_judge_failure_preserves_the_observed_response_without_claiming_quality(monkeypatch, tmp_path):
    manifest, cases = evaluation.read_cases()
    row, request = cases[0]
    _fake_live(monkeypatch, tmp_path, manifest)
    monkeypatch.setattr(evaluation, "invoke", Mock(return_value=_discovery(row)))
    monkeypatch.setattr(evaluation, "judge", AsyncMock(side_effect=RuntimeError("Synthetic judge timeout")))
    with pytest.raises(RuntimeError, match="Synthetic judge timeout"):
        evaluation.run(manifest, [(row, request)], with_judge=True)
    summary_file = next((tmp_path / manifest["resultsDirectory"]).glob("*/summary.json"))
    summary = json.loads(summary_file.read_text())
    assert summary["status"] == "failed" and summary["qualityStatus"] == "not_assessed"
    unfinished = json.loads((tmp_path / summary["unfinishedItemFile"]).read_text())
    assert unfinished["response"] == _discovery(row).model_dump_json()
    assert unfinished["semantic_review"]["result"] == "not_completed"
