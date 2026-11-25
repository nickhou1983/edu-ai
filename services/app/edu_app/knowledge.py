import re
import unicodedata
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from edu_app.db import Job, KnowledgeActivity, KnowledgePoint
from edu_domain.contracts import KnowledgeDescriptor


def normalized_title(value: str) -> str:
    value = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())
    if not value or len(value) > 160:
        raise ValueError("知识点名称需要包含文字或数字。")
    return value


def public_point(point: KnowledgePoint) -> dict:
    return {
        "id": point.id, "subject": point.subject, "title": point.title, "unit": point.unit,
        "description": point.description, "origin": point.origin, "reviewed": point.reviewed,
        "assessment": "existing" if point.origin == "builtin" else "reference",
    }


def descriptor(point: KnowledgePoint) -> KnowledgeDescriptor:
    return KnowledgeDescriptor(id=point.id, title=point.title, unit=point.unit, description=point.description)


def catalog(session: Session, subject: str) -> list[KnowledgeDescriptor]:
    # Full-title deduplication still queries the complete registry, even beyond this bounded model context.
    rows = session.scalars(
        select(KnowledgePoint).where(KnowledgePoint.subject == subject)
        .order_by((KnowledgePoint.origin == "builtin").desc(), KnowledgePoint.created_at.desc(), KnowledgePoint.id)
        .limit(250)
    )
    return [descriptor(point).model_copy(update={"description": ""}) for point in rows]


def resolve_point(session: Session, subject: str, candidate: dict, owner: str) -> KnowledgePoint:
    if existing_id := candidate["existing_id"]:
        point = session.get(KnowledgePoint, existing_id)
        if point is None or point.subject != subject:
            raise ValueError("Unknown knowledge point")
        return point
    title = candidate["title"].strip()
    normalized = normalized_title(title)
    query = select(KnowledgePoint).where(
        KnowledgePoint.subject == subject, KnowledgePoint.normalized_title == normalized
    )
    if point := session.scalar(query):
        return point
    point = KnowledgePoint(
        id=uuid4().hex, subject=subject, title=title, normalized_title=normalized,
        unit=candidate["unit"].strip(), description=candidate["description"].strip(),
        origin="discovered", reviewed=False, created_by=owner,
    )
    try:
        with session.begin_nested():
            session.add(point)
            session.flush()
    except IntegrityError:
        point = session.scalar(query)
        if point is None:
            raise
    return point


def create_discovery(session: Session, owner: str, subject: str, source_id: str, pages: list[dict]):
    activity = KnowledgeActivity(
        owner=owner, subject=subject, kind="discovery", source_id=source_id,
        document={"pages": pages, "items": []},
    )
    session.add(activity)
    session.flush()
    session.add(Job(knowledge_id=activity.id))
