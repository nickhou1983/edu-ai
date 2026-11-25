from copy import deepcopy
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from edu_app.auth import Auth
from edu_app.db import Job, KnowledgeActivity, KnowledgePoint, now_ms
from edu_app.jobs import JOB_ERRORS, RETRYABLE_FAILURES
from edu_app.knowledge import descriptor, normalized_title, public_point
from edu_app.runtime import Runtime
from edu_domain.contracts import Contract, Difficulty, EnglishText, KnowledgeTitle, Subject


class KnowledgeInput(Contract):
    subject: Subject
    title: KnowledgeTitle
    unit: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=600)]

    @field_validator("title", "unit", "description", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class ReferencePracticeInput(Contract):
    difficulty: Difficulty = "foundation"
    count: Annotated[int, Field(ge=3, le=5)] = 4


class ReferenceAnswer(Contract):
    answer: EnglishText


def _query(owner: str):
    return select(KnowledgeActivity, Job).join(
        Job, Job.knowledge_id == KnowledgeActivity.id
    ).where(KnowledgeActivity.owner == owner)


def _get(session: Session, owner: str, id: UUID, *, lock=False):
    query = _query(owner).where(KnowledgeActivity.id == str(id))
    if lock:
        query = query.with_for_update(of=KnowledgeActivity)
    row = session.execute(query).one_or_none()
    if row is None:
        raise HTTPException(404, "找不到这条学习记录。")
    return row


def _info(activity: KnowledgeActivity, job: Job):
    return {
        "id": activity.id, "jobId": job.id, "subject": activity.subject, "kind": activity.kind,
        "pointId": activity.point_id, "sourceId": activity.source_id, "createdAt": activity.created_at,
        "status": job.status, "error": JOB_ERRORS.get(job.error) if job.error else None,
        "canRetry": job.status == "failed" and job.error in RETRYABLE_FAILURES and job.manual_retries < 1,
        "eligibleForMastery": False,
    }


def knowledge_router(runtime: Runtime, auth: Auth) -> APIRouter:
    router = APIRouter(prefix="/api")
    owner_dependency = Depends(auth.current_owner)
    manager_dependency = Depends(auth.knowledge_manager)

    @router.get("/knowledge-points")
    def directory(request: Request, subject: Subject = "math", owner: str = owner_dependency):
        with runtime.db.session() as session:
            points = session.scalars(
                select(KnowledgePoint).where(KnowledgePoint.subject == subject)
                .order_by(KnowledgePoint.created_at.desc(), KnowledgePoint.id)
            )
            return {
                "canManage": auth.can_manage_knowledge(request, owner),
                "points": [public_point(point) for point in points],
            }

    @router.post("/knowledge-points", status_code=201)
    def create(body: KnowledgeInput, owner: str = manager_dependency):
        values = {name: value.strip() for name, value in body.model_dump().items()}
        try:
            normalized = normalized_title(values["title"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not values["unit"] or not values["description"]:
            raise HTTPException(422, "请填写章节和知识点说明。")
        point = KnowledgePoint(
            id=uuid4().hex, **values, normalized_title=normalized, origin="manual",
            reviewed=True, created_by=owner,
        )
        try:
            with runtime.db.transaction() as session:
                session.add(point)
                session.flush()
                return public_point(point)
        except IntegrityError:
            with runtime.db.session() as session:
                existing = session.scalar(select(KnowledgePoint).where(
                    KnowledgePoint.subject == body.subject, KnowledgePoint.normalized_title == normalized
                ))
                if existing:
                    raise HTTPException(409, "该知识点已经存在，请使用目录中的条目。") from None
            raise

    @router.patch("/knowledge-points/{point_id}")
    def maintain(point_id: str, body: KnowledgeInput, _owner: str = manager_dependency):
        values = {name: value.strip() for name, value in body.model_dump().items()}
        try:
            normalized = normalized_title(values["title"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not values["unit"] or not values["description"]:
            raise HTTPException(422, "请填写章节和知识点说明。")
        try:
            with runtime.db.transaction() as session:
                point = session.get(KnowledgePoint, point_id, with_for_update=True)
                if point is None:
                    raise HTTPException(404, "找不到这个知识点。")
                if point.origin == "builtin" or point.subject != body.subject:
                    raise HTTPException(409, "内置知识点和已关联学科不能修改。")
                for name, value in values.items():
                    setattr(point, name, value)
                point.normalized_title, point.reviewed = normalized, True
                session.flush()
                return public_point(point)
        except IntegrityError:
            with runtime.db.session() as session:
                existing = session.scalar(select(KnowledgePoint).where(
                    KnowledgePoint.subject == body.subject, KnowledgePoint.normalized_title == normalized
                ))
                if existing and existing.id != point_id:
                    raise HTTPException(409, "该名称已经存在，请保留原有知识点。") from None
            raise

    @router.post("/knowledge-points/{point_id}/practices", status_code=202)
    def practice(point_id: str, body: ReferencePracticeInput, owner: str = owner_dependency):
        with runtime.db.transaction() as session:
            point = session.get(KnowledgePoint, point_id)
            if point is None:
                raise HTTPException(404, "找不到这个知识点。")
            activity = KnowledgeActivity(
                owner=owner, subject=point.subject, kind="practice", point_id=point.id,
                document={"count": body.count, "difficulty": body.difficulty, "items": [],
                          "point": descriptor(point).model_dump()},
            )
            session.add(activity)
            session.flush()
            job = Job(knowledge_id=activity.id)
            session.add(job)
            session.flush()
            return _info(activity, job)

    @router.get("/knowledge-activities")
    def history(subject: Subject = "math", owner: str = owner_dependency):
        with runtime.db.session() as session:
            rows = session.execute(_query(owner).where(
                KnowledgeActivity.subject == subject, KnowledgeActivity.kind != "feedback"
            ).order_by(KnowledgeActivity.created_at.desc()).limit(25))
            return [_info(*row) for row in rows]

    @router.get("/knowledge-activities/{activity_id}")
    def detail(activity_id: UUID, owner: str = owner_dependency):
        with runtime.db.session() as session:
            activity, job = _get(session, owner, activity_id)
            items = []
            for item in activity.document.get("items", []):
                if activity.kind == "discovery":
                    items.append({
                        "label": item["label"], "page": item["page"], "stem": item["stem"],
                        "region": item["region"], "feedback": item.get("feedback"),
                        "points": [public_point(session.get_one(KnowledgePoint, id)) for id in item["pointIds"]],
                    })
                else:
                    exercise = item["exercise"]
                    value = {
                        "id": item["id"], "instruction": exercise["instruction"], "context": exercise["context"],
                        "answer": item["answer"], "referenceAnswer": exercise["reference_answer"] if item["answer"] else None,
                        "explanation": exercise["explanation"] if item["answer"] else None,
                        "feedback": None, "feedbackStatus": None, "feedbackError": None, "feedbackId": None,
                        "feedbackCanRetry": False,
                    }
                    if feedback_id := item.get("feedbackId"):
                        feedback, feedback_job = _get(session, owner, UUID(feedback_id))
                        value.update(
                            feedbackId=feedback.id, feedback=feedback.document.get("feedback"),
                            feedbackStatus=feedback_job.status, feedbackError=_info(feedback, feedback_job)["error"],
                            feedbackCanRetry=_info(feedback, feedback_job)["canRetry"],
                        )
                    items.append(value)
            return {
                **_info(activity, job), "items": items,
                "point": public_point(session.get_one(KnowledgePoint, activity.point_id)) if activity.point_id else None,
                "retakePages": activity.document.get("retakePages", []),
                "provenance": activity.document.get("provenance", []),
            }

    @router.post("/knowledge-activities/{activity_id}/retry", status_code=202)
    def retry(activity_id: UUID, owner: str = owner_dependency):
        with runtime.db.transaction() as session:
            activity, _ = _get(session, owner, activity_id)
            job = session.scalars(select(Job).where(Job.knowledge_id == activity.id).with_for_update()).one()
            if not _info(activity, job)["canRetry"]:
                raise HTTPException(409, "此任务不能再次处理，请重新创建。")
            job.status, job.error, job.attempts = "queued", None, 0
            job.available_at, job.dispatched_at = now_ms(), None
            job.manual_retries += 1
            return _info(activity, job)

    @router.post("/knowledge-activities/{activity_id}/items/{item_id}/answer", status_code=202)
    def answer(activity_id: UUID, item_id: UUID, body: ReferenceAnswer, owner: str = owner_dependency):
        value = body.answer.strip()
        if not value:
            raise HTTPException(422, "请先写下你的回答。")
        with runtime.db.transaction() as session:
            activity, job = _get(session, owner, activity_id, lock=True)
            if activity.kind != "practice" or job.status != "done":
                raise HTTPException(409, "请等待参考练习生成完成。")
            document = deepcopy(activity.document)
            item = next((item for item in document["items"] if item["id"] == str(item_id)), None)
            if item is None:
                raise HTTPException(404, "找不到这道练习题。")
            if item["answer"] is not None:
                if item["answer"] != value:
                    raise HTTPException(409, "这道题已经提交过，请刷新查看反馈。")
                feedback, feedback_job = _get(session, owner, UUID(item["feedbackId"]))
                return _info(feedback, feedback_job)
            feedback = KnowledgeActivity(
                owner=owner, subject=activity.subject, kind="feedback", point_id=activity.point_id,
                document={"exercise": item["exercise"], "answer": value, "point": document["point"]},
            )
            session.add(feedback)
            session.flush()
            feedback_job = Job(knowledge_id=feedback.id)
            session.add(feedback_job)
            item["answer"], item["feedbackId"] = value, feedback.id
            activity.document = document
            session.flush()
            return _info(feedback, feedback_job)

    return router
