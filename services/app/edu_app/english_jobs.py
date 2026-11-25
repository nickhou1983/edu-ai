from base64 import b64encode

from sqlalchemy.orm import Session

from edu_agent.demo import DemoImageOnly
from edu_app.agent_client import Agent, AgentFailure
from edu_app.assets import AssetNotFound, AssetUnavailable
from edu_app.db import EnglishRecord, Evidence, new_id, now_ms
from edu_app.runtime import Runtime
from edu_domain.contracts import (
    AnalyzeEnglish,
    EnglishAnalysisResult,
    EnglishFeedbackResult,
    EnglishPracticeResult,
    EnglishProblem,
    GenerateEnglish,
    ReviewEnglish,
)
from edu_domain.english import ENGLISH_RULES, english_key, english_outcome


def english_work(runtime: Runtime, agent: Agent, record_id: str) -> dict:
    with runtime.db.session() as session:
        record = session.get_one(EnglishRecord, record_id)
        document, kind, skill = dict(record.document), record.kind, record.skill
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
                    "id": new_id(), "problem": problem.model_dump(), "prompt": prompt_fields(problem),
                    "answer": None, "outcome": None, "counted": False, "feedback": None,
                })
                if len(valid) == request.count:
                    return {**document, "items": list(valid.values()), "provenance": versions}
        raise AgentFailure("practice_unfilled")
    items, versions, retake = [], [], []
    for page in document["pages"]:
        try:
            data = runtime.assets.get(page["key"])
        except AssetNotFound as exc:
            raise AgentFailure("image_missing") from exc
        except AssetUnavailable as exc:
            raise AgentFailure("asset_unavailable", retryable=True) from exc
        try:
            result = agent.invoke(AnalyzeEnglish(
                version="1", operation="analyze_english", image_base64=b64encode(data).decode("ascii"),
            ))
        except DemoImageOnly as exc:
            raise AgentFailure("demo_image_only") from exc
        if not isinstance(result, EnglishAnalysisResult):
            raise AgentFailure("agent_invalid_result", retryable=True)
        versions.append(result.provenance.model_dump())
        if result.quality == "unclear" or not result.items:
            retake.append(page["number"])
        for analyzed in result.items:
            source = analyzed.source
            problem = analyzed.problem if source.readable and result.quality == "readable" else None
            if problem and prompt_fields(problem) != (source.prompt.model_dump() if source.prompt else None):
                raise AgentFailure("agent_invalid_result", retryable=True)
            outcome = english_outcome(problem, source.answer, source.mark)
            feedback = analyzed.feedback if outcome == "feedback" else None
            if outcome == "feedback" and feedback is None:
                outcome = "undetermined"
            items.append({
                "id": new_id(), "page": page["number"], "label": source.label,
                "region": source.region.model_dump(), "prompt": source.prompt.model_dump() if source.prompt else None,
                "problem": problem.model_dump() if problem else None, "answer": source.answer,
                "outcome": outcome, "counted": outcome in ("correct", "incorrect"),
                "feedback": feedback.model_dump() if feedback else None,
            })
    return {**document, "items": items, "provenance": versions, "retakePages": retake}


def prompt_fields(problem: EnglishProblem) -> dict:
    return problem.model_dump(include={"skill", "mode", "instruction", "context", "choices"})


def english_evidence(session: Session, record: EnglishRecord, item: dict):
    if item["counted"]:
        problem = EnglishProblem.model_validate(item["problem"])
        session.add(Evidence(
            source_id=item["id"], owner=record.owner, skill=problem.skill, problem_key=english_key(problem),
            score=2 if item["outcome"] == "correct" else 0, occurred_at=now_ms(), english_id=record.id,
        ))


def save_english(session: Session, record: EnglishRecord, document: dict):
    record.document = {**document, "rules": ENGLISH_RULES}
    if record.kind == "worksheet":
        for item in document["items"]:
            english_evidence(session, record, item)
