from base64 import b64encode

from sqlalchemy.orm import Session

from edu_agent.demo import DemoImageOnly
from edu_app.agent_client import Agent, AgentFailure
from edu_app.assets import AssetNotFound, AssetUnavailable
from edu_app.db import KnowledgeActivity, new_id
from edu_app.knowledge import catalog, resolve_point
from edu_app.runtime import Runtime
from edu_domain.contracts import (
    DiscoverKnowledge,
    GenerateKnowledge,
    KnowledgeDescriptor,
    KnowledgeDiscoveryResult,
    KnowledgeFeedbackResult,
    KnowledgePracticeResult,
    ReviewKnowledge,
)


def knowledge_work(runtime: Runtime, agent: Agent, activity_id: str) -> dict:
    with runtime.db.session() as session:
        activity = session.get_one(KnowledgeActivity, activity_id)
        document, subject, kind = dict(activity.document), activity.subject, activity.kind
        points = catalog(session, subject)
        point = KnowledgeDescriptor.model_validate(document["point"]) if activity.point_id else None
    if kind == "practice":
        if point is None:
            raise AgentFailure("agent_invalid_result")
        result = agent.invoke(GenerateKnowledge(
            version="1", operation="generate_knowledge", subject=subject, point=point,
            difficulty=document["difficulty"], count=document["count"],
        ))
        if not isinstance(result, KnowledgePracticeResult) or len(result.items) != document["count"]:
            raise AgentFailure("agent_invalid_result", retryable=True)
        keys = {(item.instruction.strip(), item.context.strip()) for item in result.items}
        if len(keys) != len(result.items):
            raise AgentFailure("practice_unfilled")
        return {**document, "items": [
            {"id": new_id(), "exercise": item.model_dump(), "answer": None}
            for item in result.items
        ], "provenance": [result.provenance.model_dump()]}
    if kind == "feedback":
        if point is None:
            raise AgentFailure("agent_invalid_result")
        result = agent.invoke(ReviewKnowledge(
            version="1", operation="review_knowledge", subject=subject, point=point,
            exercise=document["exercise"], answer=document["answer"],
        ))
        if not isinstance(result, KnowledgeFeedbackResult):
            raise AgentFailure("agent_invalid_result", retryable=True)
        return {**document, "feedback": result.feedback.model_dump(),
                "provenance": [result.provenance.model_dump()]}
    if kind != "discovery":
        raise AgentFailure("agent_invalid_result")
    items, versions, retake = [], [], []
    for page in document["pages"]:
        try:
            image = runtime.assets.get(page["key"])
        except AssetNotFound as exc:
            raise AgentFailure("image_missing") from exc
        except AssetUnavailable as exc:
            raise AgentFailure("asset_unavailable", retryable=True) from exc
        try:
            result = agent.invoke(DiscoverKnowledge(
                version="1", operation="discover_knowledge", subject=subject,
                image_base64=b64encode(image).decode("ascii"), catalog=points,
            ))
        except DemoImageOnly as exc:
            raise AgentFailure("demo_image_only") from exc
        if not isinstance(result, KnowledgeDiscoveryResult):
            raise AgentFailure("agent_invalid_result", retryable=True)
        allowed = {point.id for point in points}
        if any(point.existing_id and point.existing_id not in allowed
               for item in result.items for point in item.points):
            raise AgentFailure("agent_invalid_result", retryable=True)
        versions.append(result.provenance.model_dump())
        if result.quality == "unclear":
            retake.append(page["number"])
            continue
        items.extend({**item.model_dump(), "page": page["number"]} for item in result.items)
    return {**document, "items": items, "provenance": versions, "retakePages": retake}


def save_knowledge(session: Session, activity: KnowledgeActivity, document: dict):
    if activity.kind == "discovery":
        for item in document["items"]:
            try:
                points = [
                    resolve_point(session, activity.subject, candidate, activity.owner)
                    for candidate in item.pop("points")
                ]
            except ValueError as exc:
                raise AgentFailure("agent_invalid_result", retryable=True) from exc
            item["pointIds"] = list(dict.fromkeys(point.id for point in points))
    # Reference assistance never writes mastery evidence, including for built-in points.
    activity.document = document
