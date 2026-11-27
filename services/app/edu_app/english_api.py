import logging
from copy import deepcopy
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from edu_agent.english_demo import english_demo_page
from edu_app.assets import AssetNotFound, AssetUnavailable
from edu_app.db import EnglishRecord, Job, new_id, now_ms
from edu_app.english_jobs import english_evidence, has_pending_english_review
from edu_app.jobs import JOB_ERRORS, RETRYABLE_FAILURES, can_reanalyze, queue_reanalysis
from edu_app.mastery import overview
from edu_app.runtime import Runtime
from edu_domain.contracts import (
    ENGLISH_OBJECTIVE_SKILLS,
    MAX_PAGES,
    MAX_UPLOAD_BYTES,
    Contract,
    Difficulty,
    EnglishProblem,
    EnglishSkillId,
    EnglishText,
)
from edu_domain.english import ENGLISH_RULES, ENGLISH_SKILL_BY_ID, ENGLISH_SKILLS, english_correct
from edu_domain.images import ImageError, normalize_page

logger = logging.getLogger(__name__)
_NOT_FOUND = "没有找到这条记录。"


class CreateEnglishPractice(Contract):
    skill: EnglishSkillId
    difficulty: Difficulty = "foundation"
    count: Annotated[int, Field(ge=3, le=5)] = 4


class EnglishAnswer(Contract):
    answer: EnglishText


def _can_reanalyze(record: EnglishRecord, job: Job) -> bool:
    return record.kind == "worksheet" and "provenance" in record.document and can_reanalyze(job)


def _info(record: EnglishRecord, job: Job) -> dict:
    pending_review = record.kind == "worksheet" and has_pending_english_review(record.document)
    error = JOB_ERRORS.get(job.error, "任务未完成，请稍后重试。") if job.status == "failed" else None
    if error and job.reanalysis:
        error = f"重新分析未完成，已保留原报告。{error}"
    return {
        "id": record.id, "kind": record.kind, "skill": record.skill, "createdAt": record.created_at,
        "jobId": job.id, "status": job.status,
        "error": error,
        "canRetry": job.status == "failed" and job.error in RETRYABLE_FAILURES
        and job.manual_retries == 0 and not job.reanalysis,
        "canRetryReview": job.status == "done" and pending_review and job.manual_retries == 0,
        "canReanalyze": _can_reanalyze(record, job),
        "isReanalyzing": job.reanalysis and job.status in ("queued", "running"),
    }


def _query(owner: str):
    return select(EnglishRecord, Job).join(Job, Job.english_id == EnglishRecord.id).where(EnglishRecord.owner == owner)


def _get(session: Session, owner: str, record_id: UUID, *, lock=False):
    query = _query(owner).where(EnglishRecord.id == str(record_id))
    row = session.execute(query.with_for_update(of=EnglishRecord) if lock else query).one_or_none()
    if row is None:
        raise HTTPException(404, _NOT_FOUND)
    return row


def _public_item(session: Session, record: EnglishRecord, item: dict) -> dict:
    assessment_id = item.get("assessmentId")
    assessment_status = None
    if assessment_id:
        assessment, assessment_job = session.execute(_query(record.owner).where(
            EnglishRecord.id == assessment_id, EnglishRecord.kind == "assessment",
        )).one()
        assessment_status = _info(assessment, assessment_job)
        if assessment_job.status == "done":
            item = {**item, **assessment.document["result"]}
    problem = EnglishProblem.model_validate(item["problem"]) if item.get("problem") else None
    answered = record.kind == "worksheet" or (
        item["answer"] is not None and (assessment_status is None or assessment_status["status"] == "done")
    )
    result = {
        name: item.get(name)
        for name in (
            "id", "page", "label", "region", "prompt", "answer", "outcome", "counted", "feedback",
            "reason", "masterySkill",
        )
    }
    result["verification"] = item.get("verification", "legacy_match" if item.get("counted") else None)
    result["expected"] = problem.answers if problem and answered and item["outcome"] != "undetermined" else None
    result["explanation"] = (item.get("explanation") or problem.explanation) if problem and answered else None
    if assessment_status:
        result.update(
            assessmentId=assessment_id, assessmentStatus=assessment_status["status"],
            assessmentError=assessment_status["error"], assessmentCanRetry=assessment_status["canRetry"],
        )
    result["feedbackStatus"] = result["feedbackError"] = None
    if feedback_id := item.get("feedbackId"):
        row = session.execute(_query(record.owner).where(EnglishRecord.id == feedback_id)).one()
        feedback_record, feedback_job = row
        result["feedbackId"] = feedback_record.id
        result["feedbackStatus"] = feedback_job.status
        result["feedbackError"] = _info(*row)["error"]
        result["feedbackCanRetry"] = _info(*row)["canRetry"]
        result["feedback"] = feedback_record.document.get("feedback")
    return result


def english_router(runtime: Runtime, owner_dependency) -> APIRouter:
    router = APIRouter(prefix="/api/english")
    require_owner = Depends(owner_dependency)

    @router.get("/skills")
    def skills(owner: str = require_owner):
        with runtime.db.session() as session:
            result = overview(session, owner, ENGLISH_SKILLS, subject="english", include_ai=True)
            for item in result:
                item["hint"] = ENGLISH_SKILL_BY_ID[item["id"]].hint
                if item["id"] not in ENGLISH_OBJECTIVE_SKILLS:
                    item.update(status="feedback_only", percent=None, sampleCount=0, evidence=[])
            return result

    @router.get("/sample")
    def sample(_owner: str = require_owner):
        if runtime.settings.mode != "local":
            raise HTTPException(404, _NOT_FOUND)
        return Response(english_demo_page(), media_type="image/png")

    @router.post("/worksheets", status_code=202)
    def upload(files: Annotated[list[UploadFile], File(...)], owner: str = require_owner):
        if not 1 <= len(files) <= MAX_PAGES:
            raise HTTPException(422, f"请选择 1–{MAX_PAGES} 张英语作业图片。")
        try:
            images = [normalize_page(file.file.read(MAX_UPLOAD_BYTES + 1)) for file in files]
        except ImageError as exc:
            raise HTTPException(422, str(exc)) from exc
        hashes = [sha256(image).digest() for image in images]
        if len(set(hashes)) != len(hashes):
            raise HTTPException(422, "有重复的图片，请每页只保留一次。")
        digest = sha256(b"".join(hashes)).digest()
        query = _query(owner).where(EnglishRecord.digest == digest)
        with runtime.db.session() as session:
            if row := session.execute(query).one_or_none():
                return _info(*row)
        record = EnglishRecord(
            id=new_id(), owner=owner, kind="worksheet", digest=digest,
            document={"pages": [], "items": []},
        )
        pages = [{"number": i, "key": f"{record.id}/{i}.jpg"} for i in range(1, len(images) + 1)]
        stored = []

        def cleanup():
            for key in stored:
                try:
                    runtime.assets.delete(key)
                except AssetUnavailable:
                    logger.warning("english_asset_cleanup_failed record=%s", record.id)

        try:
            for page, data in zip(pages, images, strict=True):
                runtime.assets.put(page["key"], data)
                stored.append(page["key"])
        except AssetUnavailable:
            cleanup()
            raise
        try:
            with runtime.db.transaction() as session:
                record.document = {"pages": pages, "items": [], "aiMastery": runtime.settings.english_ai_mastery_enabled}
                session.add(record)
                session.flush()
                job = Job(english_id=record.id)
                session.add(job)
                from edu_app.knowledge import create_discovery

                create_discovery(session, owner, "english", record.id, pages)
                session.flush()
                return _info(record, job)
        except IntegrityError:
            cleanup()
            with runtime.db.session() as session:
                if row := session.execute(query).one_or_none():
                    return _info(*row)
            raise
        except SQLAlchemyError:
            cleanup()
            raise

    @router.post("/practices", status_code=202)
    def generate(body: CreateEnglishPractice, owner: str = require_owner):
        with runtime.db.transaction() as session:
            current = next(s for s in overview(session, owner, ENGLISH_SKILLS, subject="english", include_ai=True)
                           if s["id"] == body.skill)
            record = EnglishRecord(owner=owner, kind="practice", skill=body.skill, document={
                "count": body.count, "difficulty": body.difficulty, "items": [],
                "purpose": "diagnose" if current["status"] == "insufficient" else "strengthen",
                "aiMastery": runtime.settings.english_ai_mastery_enabled,
            })
            session.add(record)
            session.flush()
            job = Job(english_id=record.id)
            session.add(job)
            session.flush()
            return _info(record, job)

    @router.get("/records")
    def history(owner: str = require_owner):
        with runtime.db.session() as session:
            return [_info(*row) for row in session.execute(
                _query(owner).where(EnglishRecord.kind.in_(("worksheet", "practice")))
                .order_by(EnglishRecord.created_at.desc()).limit(25)
            )]

    @router.get("/records/{record_id}")
    def detail(record_id: UUID, owner: str = require_owner):
        with runtime.db.session() as session:
            record, job = _get(session, owner, record_id)
            document = record.document
            return {
                **_info(record, job),
                "pages": [p["number"] for p in document.get("pages", [])],
                "items": [_public_item(session, record, item) for item in document.get("items", [])],
                "count": document.get("count"), "difficulty": document.get("difficulty"),
                "purpose": document.get("purpose"), "provenance": document.get("provenance", []),
                "retakePages": document.get("retakePages", []),
                "feedback": document.get("feedback"),
            }

    @router.get("/records/{record_id}/pages/{number}")
    def original(record_id: UUID, number: int, owner: str = require_owner):
        with runtime.db.session() as session:
            record, _ = _get(session, owner, record_id)
            page = next((p for p in record.document.get("pages", []) if p["number"] == number), None)
            if not page:
                raise HTTPException(404, _NOT_FOUND)
        try:
            return Response(runtime.assets.get(page["key"]), media_type="image/jpeg")
        except AssetNotFound as exc:
            raise HTTPException(404, "原图已经找不到了，请重拍后上传。") from exc

    @router.post("/records/{record_id}/retry", status_code=202)
    def retry(record_id: UUID, owner: str = require_owner):
        with runtime.db.transaction() as session:
            record, job = _get(session, owner, record_id)
            session.refresh(job, with_for_update=True)
            session.refresh(record, with_for_update=True)
            info = _info(record, job)
            if not info["canRetry"] and not info["canRetryReview"]:
                raise HTTPException(409, "此任务不能再次处理，请检查后重新创建。")
            if info["canRetryReview"]:
                record.document = {**record.document, "pendingReviewRetry": True}
            job.status, job.error, job.attempts = "queued", None, 0
            job.available_at, job.dispatched_at = now_ms(), None
            job.manual_retries += 1
            return _info(record, job)

    @router.post("/records/{record_id}/reanalyze", status_code=202)
    def reanalyze_worksheet(record_id: UUID, owner: str = require_owner):
        with runtime.db.transaction() as session:
            record, job = _get(session, owner, record_id)
            session.refresh(job, with_for_update=True)
            session.refresh(record, with_for_update=True)
            if not _can_reanalyze(record, job):
                raise HTTPException(409, "此记录当前无法重新分析，请刷新查看最新状态。")
            record.document = {
                key: value for key, value in record.document.items()
                if key not in ("pageCheckpoints", "pendingReviewRetry")
            }
            queue_reanalysis(job)
            return _info(record, job)

    @router.post("/records/{record_id}/items/{item_id}/answer")
    def answer(record_id: UUID, item_id: UUID, body: EnglishAnswer, owner: str = require_owner):
        value = body.answer.strip()
        if not value:
            raise HTTPException(422, "请先写下你的回答。")
        with runtime.db.transaction() as session:
            record, job = _get(session, owner, record_id, lock=True)
            if record.kind != "practice" or job.status != "done":
                raise HTTPException(409, "请等待英语练习生成完成。")
            document = deepcopy(record.document)
            item = next((item for item in document["items"] if item["id"] == str(item_id)), None)
            if item is None:
                raise HTTPException(404, _NOT_FOUND)
            if item["answer"] is not None:
                if item["answer"] != value:
                    raise HTTPException(409, "这道题已经提交过，刷新即可查看结果。")
                return _public_item(session, record, item)
            if document.get("rules") != ENGLISH_RULES:
                raise HTTPException(409, "题目规则已更新，请生成新的练习。")
            problem = EnglishProblem.model_validate(item["problem"])
            if problem.mode != "open" and len(value) > 256:
                raise HTTPException(422, "客观题请填写简短答案，或选择一个选项。")
            if problem.mode == "choice" and value.upper() not in list("ABCD"[:len(problem.choices)]):
                raise HTTPException(422, "请选择一个选项。")
            item["answer"] = value
            if problem.mode == "open":
                feedback = EnglishRecord(
                    owner=owner, kind="feedback", skill=problem.skill,
                    document={"prompt": item["prompt"], "answer": value},
                )
                session.add(feedback)
                session.flush()
                session.add(Job(english_id=feedback.id))
                item["feedbackId"] = feedback.id
                item["outcome"] = "feedback"
            elif document.get("aiMastery"):
                assessment = EnglishRecord(
                    owner=owner, kind="assessment", skill=problem.skill,
                    document={"practiceId": record.id, "itemId": item["id"]},
                )
                session.add(assessment)
                session.flush()
                session.add(Job(english_id=assessment.id))
                item["assessmentId"] = assessment.id
            else:
                item["outcome"] = "correct" if english_correct(problem, value) else "incorrect"
                item["counted"] = True
                english_evidence(session, record, item)
            record.document = document
            session.flush()
            return _public_item(session, record, item)

    return router
