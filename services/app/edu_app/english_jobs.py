from base64 import b64encode
from collections.abc import Callable
from itertools import batched

from sqlalchemy.orm import Session

from edu_agent.demo import DemoImageOnly
from edu_app.agent_client import Agent, AgentFailure
from edu_app.assets import AssetNotFound, AssetUnavailable
from edu_app.db import EnglishRecord, Evidence, new_id, now_ms
from edu_app.runtime import Runtime
from edu_domain.contracts import (
    MAX_AI_REVIEW_BATCH,
    AnalyzeEnglish,
    EnglishAnalysisResult,
    EnglishAnalyzedItem,
    EnglishFeedbackResult,
    EnglishGradeResult,
    EnglishPracticeResult,
    EnglishProblem,
    EnglishReadItem,
    GenerateEnglish,
    GradeEnglish,
    ReviewEnglish,
)
from edu_domain.english import (
    ENGLISH_RULES,
    EnglishGrade,
    english_key,
    english_outcome,
    english_prompt,
    grade_english,
)


def english_work(
    runtime: Runtime, agent: Agent, record_id: str, *, renew: Callable[[], None], checkpoint: Callable[[dict], None],
) -> dict:
    with runtime.db.session() as session:
        record = session.get_one(EnglishRecord, record_id)
        document, kind, skill = dict(record.document), record.kind, record.skill
        if kind == "assessment":
            _, item = _assessment_target(session, record, document)
    if kind == "assessment":
        problem = EnglishProblem.model_validate(item["problem"])
        prompt = english_prompt(problem)
        try:
            result = agent.invoke(GradeEnglish(version="2", operation="grade_english", prompt=prompt, answer=item["answer"]))
        except DemoImageOnly as exc:
            raise AgentFailure("review_unavailable") from exc
        if not isinstance(result, EnglishGradeResult):
            raise AgentFailure("agent_invalid_result", retryable=True)
        grade = grade_english(prompt, item["answer"], "present", result.assessment, index=0, published=problem)
        return {**document, "result": _grade_fields(grade), "provenance": [result.provenance.model_dump()]}
    if kind == "feedback":
        result = agent.invoke(ReviewEnglish(
            version="1", operation="review_english", prompt=document["prompt"], answer=document["answer"],
        ))
        if not isinstance(result, EnglishFeedbackResult):
            raise AgentFailure("agent_invalid_result", retryable=True)
        return {**document, "feedback": result.feedback.model_dump(), "provenance": [result.provenance.model_dump()]}
    if kind == "practice":
        request = GenerateEnglish(
            version="1", operation="generate_english", skill=skill,
            difficulty=document["difficulty"], count=document["count"],
        )
        valid = {}
        versions = []
        for _ in range(3):
            result = agent.invoke(request)
            if not isinstance(result, EnglishPracticeResult):
                raise AgentFailure("agent_invalid_result", retryable=True)
            versions.append(result.provenance.model_dump())
            for problem in result.items:
                if problem.skill != skill:
                    continue
                valid.setdefault(english_key(problem), {
                    "id": new_id(), "problem": problem.model_dump(), "prompt": english_prompt(problem).model_dump(),
                    "answer": None, "outcome": None, "counted": False, "feedback": None,
                })
                if len(valid) == request.count:
                    return {**document, "items": list(valid.values()), "provenance": versions}
        raise AgentFailure("practice_unfilled")
    if document.get("pendingReviewRetry"):
        return _retry_pending(runtime, agent, document, renew, checkpoint)
    return _analyze(runtime, agent, document, renew, checkpoint)


def _analyze(
    runtime: Runtime, agent: Agent, document: dict, renew: Callable[[], None], checkpoint: Callable[[dict], None],
) -> dict:
    ai = document.get("aiMastery", False)
    completed = dict(document.get("pageCheckpoints", {}))
    for page in document["pages"]:
        number = str(page["number"])
        if number in completed:
            continue
        renew()
        result = _read_page(agent, _image(runtime, page["key"]), ai=ai)
        completed[number] = {
            "items": [
                _report_item(result, item, page["number"], index, new_id(), ai=ai)
                for index, item in enumerate(result.items)
            ],
            "provenance": result.provenance.model_dump(),
            "retake": result.quality == "unclear" or not result.items,
        }
        if ai:
            checkpoint({**document, "pageCheckpoints": completed})
    document.pop("pageCheckpoints", None)
    ordered = [(p["number"], completed[str(p["number"])]) for p in document["pages"]]
    return {
        **document, "items": [item for _, page in ordered for item in page["items"]],
        "provenance": [page["provenance"] for _, page in ordered],
        "retakePages": [number for number, page in ordered if page["retake"]],
    }


def _image(runtime: Runtime, key: str) -> str:
    try:
        return b64encode(runtime.assets.get(key)).decode("ascii")
    except AssetNotFound as exc:
        raise AgentFailure("image_missing") from exc
    except AssetUnavailable as exc:
        raise AgentFailure("asset_unavailable", retryable=True) from exc


def _read_page(
    agent: Agent, image: str, *, ai: bool, sources: list[EnglishReadItem] | None = None,
) -> EnglishAnalysisResult:
    request = AnalyzeEnglish(
        version="2" if ai else "1", operation="analyze_english", image_base64=image, review_items=sources,
    )
    try:
        result = agent.invoke(request)
    except DemoImageOnly as exc:
        raise AgentFailure("review_unavailable" if sources is not None else "demo_image_only") from exc
    if not isinstance(result, EnglishAnalysisResult) or result.version != request.version:
        raise AgentFailure("agent_invalid_result", retryable=True)
    if sources is not None and (result.quality != "readable" or [item.source for item in result.items] != sources):
        raise AgentFailure("agent_invalid_result", retryable=True)
    return result


def _grade_fields(grade: EnglishGrade) -> dict:
    return {
        "outcome": grade.outcome, "reason": grade.reason,
        "problem": grade.reference.model_dump() if grade.reference else None, "explanation": grade.explanation,
        "masterySkill": grade.mastery_skill, "score": grade.score, "counted": grade.score is not None,
        "verification": "ai_review" if grade.reference else "unresolved",
    }


def _report_item(
    result: EnglishAnalysisResult, analyzed: EnglishAnalyzedItem, page: int, index: int, item_id: str, *, ai: bool,
) -> dict:
    source = analyzed.source
    prompt = source.prompt
    readable = source.readable and result.quality == "readable"
    item = {
        "id": item_id, "page": page, "label": source.label, "region": source.region.model_dump(),
        "prompt": prompt.model_dump() if prompt else None, "answer": source.answer, "feedback": None,
    }
    if ai and readable and prompt and prompt.mode != "open":
        if analyzed.problem is not None or analyzed.feedback is not None:
            raise AgentFailure("agent_invalid_result", retryable=True)
        if analyzed.review_timed_out:
            return {
                **item, **_grade_fields(EnglishGrade("undetermined", "review_timeout")),
                "reviewSource": source.model_dump(),
            }
        return {**item, **_grade_fields(grade_english(prompt, source.answer, source.mark, analyzed.assessment, index=index))}
    problem = analyzed.problem if readable else None
    if problem and (english_prompt(problem) != prompt or (ai and problem.mode != "open")):
        raise AgentFailure("agent_invalid_result", retryable=True)
    outcome = english_outcome(problem, source.answer, source.mark)
    feedback = analyzed.feedback if outcome == "feedback" else None
    reason = None
    if outcome == "feedback" and feedback is None:
        outcome, reason = "undetermined", "feedback_unavailable"
    return {
        **item, "problem": problem.model_dump() if problem else None,
        "outcome": outcome, "counted": outcome in ("correct", "incorrect"),
        "feedback": feedback.model_dump() if feedback else None, "reason": reason,
    }


def has_pending_english_review(document: dict) -> bool:
    return any(item.get("reviewSource") is not None for item in document.get("items", ()))


def _retry_pending(
    runtime: Runtime, agent: Agent, document: dict, renew: Callable[[], None], checkpoint: Callable[[dict], None],
) -> dict:
    document = {**document, "items": list(document["items"]), "provenance": list(document["provenance"])}
    for page in document["pages"]:
        targets = [
            (i, item) for i, item in enumerate(document["items"])
            if item["page"] == page["number"] and item.get("reviewSource") is not None
        ]
        if not targets:
            continue
        renew()
        image = _image(runtime, page["key"])
        for batch in batched(targets, MAX_AI_REVIEW_BATCH):
            renew()
            sources = [EnglishReadItem.model_validate(item["reviewSource"]) for _, item in batch]
            result = _read_page(agent, image, ai=True, sources=sources)
            if any(item.review_timed_out for item in result.items):
                raise AgentFailure("agent_timeout", retryable=True)
            for index, ((position, original), replacement) in enumerate(zip(batch, result.items, strict=True)):
                document["items"][position] = _report_item(
                    result, replacement, page["number"], index, original["id"], ai=True,
                )
            document["provenance"].append(result.provenance.model_dump())
            checkpoint(document)
    del document["pendingReviewRetry"]
    return document


def english_evidence(session: Session, record: EnglishRecord, item: dict, *, occurred_at: int | None = None):
    if item["counted"]:
        problem = EnglishProblem.model_validate(item["problem"])
        verification = item.get("verification", "rules")
        session.add(Evidence(
            source_id=item["id"], owner=record.owner, skill=problem.skill, problem_key=english_key(problem),
            score=item["score"] if verification == "ai_review" else (2 if item["outcome"] == "correct" else 0),
            verification=verification, occurred_at=occurred_at if occurred_at is not None else now_ms(),
            english_id=record.id,
        ))


def _assessment_target(session: Session, record: EnglishRecord, document: dict) -> tuple[EnglishRecord, dict]:
    parent = session.get(EnglishRecord, document["practiceId"])
    if parent and parent.owner == record.owner and parent.kind == "practice" and parent.document.get("aiMastery"):
        for item in parent.document["items"]:
            if item["id"] == document["itemId"] and item.get("assessmentId") == record.id and item["answer"]:
                return parent, item
    raise AgentFailure("agent_invalid_result")


def save_english(session: Session, record: EnglishRecord, document: dict):
    if record.kind == "worksheet":
        counted = {item["id"] for item in record.document["items"] if item.get("counted")}
        for item in document["items"]:
            if item["id"] not in counted:
                english_evidence(
                    session, record, item, occurred_at=record.created_at if document.get("aiMastery") else None,
                )
    elif record.kind == "assessment":
        parent, item = _assessment_target(session, record, document)
        english_evidence(session, parent, {**item, **document["result"]}, occurred_at=record.created_at)
    record.document = {**document, "rules": ENGLISH_RULES}
