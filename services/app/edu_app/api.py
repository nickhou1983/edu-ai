import logging
from contextlib import asynccontextmanager
from hashlib import sha256
from threading import Event, Thread
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from edu_agent.demo import demo_page
from edu_app.assets import AssetNotFound, AssetUnavailable
from edu_app.auth import Auth
from edu_app.db import Evidence, Job, Page, Practice, PracticeItem, Report, Submission, new_id, now_ms
from edu_app.english_api import english_router
from edu_app.jobs import (
    JOB_ERRORS,
    RETRYABLE_FAILURES,
    can_reanalyze,
    clear_page_checkpoints,
    queue_reanalysis,
)
from edu_app.mastery import overview
from edu_app.queue import dispatch_loop
from edu_app.reports import ReportItem, is_pending_review, public_report, save_report
from edu_app.runtime import Runtime
from edu_domain.contracts import (
    MAX_PAGES,
    MAX_UPLOAD_BYTES,
    Contract,
    Difficulty,
    Expression,
    ProblemSpec,
    SkillId,
)
from edu_domain.curriculum import SKILL_BY_ID
from edu_domain.grading import grade_item
from edu_domain.images import ImageError, normalize_page
from edu_domain.math import MathError, check_answer, problem_text, verify

logger = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
_NOT_FOUND = "没有找到这条记录。"


class Correction(Contract):
    answer: Expression
    revision: Annotated[int, Field(ge=1)]


class CreatePractice(Contract):
    skill: SkillId
    count: Annotated[int, Field(ge=3, le=5)] = 4
    difficulty: Difficulty = "foundation"


class Answer(Contract):
    answer: Expression


class LimitBody:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PATCH"):
            return await self.app(scope, receive, send)
        image_upload = scope["path"] in ("/api/submissions", "/api/english/worksheets")
        limit = MAX_PAGES * MAX_UPLOAD_BYTES + 65_536 if image_upload else 16_384
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > limit:
                raise HTTPException(413, "请求太大，请减少图片数量或压缩后重试。")
            return message

        return await self.app(scope, limited_receive, send)


def _job_info(job: Job) -> dict:
    return {
        "jobId": job.id,
        "status": job.status,
        "error": JOB_ERRORS.get(job.error, "任务未完成，请稍后重试。") if job.status == "failed" else None,
    }


def _can_retry(submission: Submission, job: Job, report: Report | None) -> bool:
    if job.manual_retries or job.reanalysis:
        return False
    if not submission.revision:
        return job.status == "failed" and job.error in RETRYABLE_FAILURES
    return job.status == "done" and report is not None and any(
        is_pending_review(item) for item in report.document["items"]
    )


def _submission_info(submission: Submission, job: Job, report: Report | None = None) -> dict:
    info = _job_info(job)
    if job.status == "failed":
        if job.reanalysis:
            info["error"] = f"重新分析未完成，已保留原报告。{info['error']}"
        elif submission.revision:
            if job.error == "agent_timeout":
                info["error"] = "复核超时，已保留现有报告。"
            else:
                reason = {
                    "agent_no_result": "复核服务没有返回可用结果。",
                    "agent_invalid_result": "复核结果不完整或无法核对。",
                    "agent_rate_limited": "复核服务请求过多。",
                    "agent_unavailable": "复核服务暂时不可用。",
                }.get(job.error, info["error"])
                info["error"] = f"复核未完成，已保留现有报告。{reason}"
        elif job.error == "agent_timeout":
            info["error"] = "分析超时。原图仍在，无需重新上传。"
    return {
        "id": submission.id,
        "createdAt": submission.created_at,
        "revision": submission.revision,
        "canRetry": _can_retry(submission, job, report),
        "canReanalyze": bool(submission.revision) and can_reanalyze(job),
        "isReanalyzing": job.reanalysis and job.status in ("queued", "running"),
        **info,
    }


def _submission(session: Session, owner: str, submission_id: UUID, *, lock: bool = False) -> Submission:
    query = select(Submission).where(Submission.id == str(submission_id), Submission.owner == owner)
    item = session.scalar(query.with_for_update() if lock else query)
    if not item:
        raise HTTPException(404, _NOT_FOUND)
    return item


def _practice_info(practice: Practice, job: Job) -> dict:
    return {
        "id": practice.id,
        "skill": practice.skill,
        "difficulty": practice.difficulty,
        "purpose": practice.purpose,
        "count": practice.count,
        "createdAt": practice.created_at,
        **_job_info(job),
    }


def _practice_item(
    session: Session, owner: str, practice_id: UUID, item_id: UUID
) -> tuple[PracticeItem, Practice]:
    row = session.execute(
        select(PracticeItem, Practice)
        .join(Practice, PracticeItem.practice_id == Practice.id)
        .where(Practice.owner == owner, Practice.id == str(practice_id), PracticeItem.id == str(item_id))
        .with_for_update(of=PracticeItem)
    ).one_or_none()
    if row is None:
        raise HTTPException(404, _NOT_FOUND)
    return row[0], row[1]


def _answer_result(item: PracticeItem) -> dict | None:
    if item.score is None:
        return None
    return {
        "answer": item.answer,
        "correct": item.score == 2,
        "expected": item.expected,
        "explanation": item.explanation,
        "eligibleForMastery": item.counted,
    }


def _public_practice_item(item: PracticeItem) -> dict:
    spec = ProblemSpec.model_validate(item.spec)
    return {
        "id": item.id,
        "position": item.position,
        "stem": problem_text(spec),
        "choices": spec.choices,
        "hintSeen": item.hint_seen,
        "result": _answer_result(item),
    }


def build_app(runtime: Runtime) -> FastAPI:
    settings = runtime.settings
    auth = Auth(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = Event()
        dispatcher = None
        if settings.mode == "azure":
            dispatcher = Thread(target=dispatch_loop, args=(runtime, stop), name="outbox", daemon=True)
            dispatcher.start()
        try:
            yield
        finally:
            stop.set()
            if dispatcher:
                dispatcher.join(timeout=15)

    app = FastAPI(title="Edu AI", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(LimitBody)
    if settings.mode == "local":
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    require_owner = Depends(auth.current_owner)
    app.include_router(english_router(runtime, auth.current_owner))
    from edu_app.knowledge_api import knowledge_router

    app.include_router(knowledge_router(runtime, auth))

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.update(_NO_STORE)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return JSONResponse({"detail": "输入格式不正确，请检查后重试。"}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(_request: Request, exc: SQLAlchemyError):
        logger.error("database_request_failed type=%s", type(exc).__name__)
        return JSONResponse({"detail": "数据服务暂不可用，请稍后重试。"}, status_code=503)

    @app.exception_handler(AssetUnavailable)
    async def asset_unavailable(_request: Request, _exc: AssetUnavailable):
        logger.warning("asset_request_failed")
        return JSONResponse({"detail": "图片服务暂不可用，请稍后重试。"}, status_code=503)

    @app.get("/api/health")
    def health():
        from edu_app.db import KnowledgeActivity, KnowledgePoint

        with runtime.db.session() as session:
            session.execute(select(Job.id, Job.knowledge_id, Job.reanalysis).limit(0))
            session.execute(select(KnowledgePoint.id).limit(0))
            session.execute(select(KnowledgeActivity.id).limit(0))
            session.execute(select(Submission.ai_mastery).limit(0))
            session.execute(select(Evidence.verification).limit(0))
        return {"status": "ready"}

    @app.get("/api/session")
    def session_config():
        return auth.public_config()

    @app.get("/api/demo/sample")
    def sample(_owner: str = require_owner):
        if settings.mode != "local":
            raise HTTPException(404, _NOT_FOUND)
        return Response(demo_page(), media_type="image/png")

    @app.post("/api/submissions", status_code=202)
    def upload(files: Annotated[list[UploadFile], File(...)], owner: str = require_owner):
        if not 1 <= len(files) <= MAX_PAGES:
            raise HTTPException(422, f"请选择 1–{MAX_PAGES} 张作业图片。")
        try:
            data = [normalize_page(file.file.read(MAX_UPLOAD_BYTES + 1)) for file in files]
        except ImageError as exc:
            raise HTTPException(422, str(exc)) from exc
        page_hashes = [sha256(image).digest() for image in data]
        if len(set(page_hashes)) != len(page_hashes):
            raise HTTPException(422, "有重复的图片，请每页只保留一次。")
        digest = sha256(b"".join(page_hashes)).digest()
        existing_query = (
            select(Submission, Job)
            .join(Job, Job.submission_id == Submission.id)
            .where(
                Submission.owner == owner,
                Submission.digest == digest,
            )
        )
        with runtime.db.session() as session:
            if existing := session.execute(existing_query).one_or_none():
                return _submission_info(*existing)

        submission = Submission(id=new_id(), owner=owner, digest=digest, ai_mastery=settings.math_ai_mastery_enabled)
        keys = [f"{submission.id}/{index}.jpg" for index in range(1, len(data) + 1)]
        stored = []

        def clean_stored():
            for key in stored:
                try:
                    runtime.assets.delete(key)
                except AssetUnavailable:
                    logger.warning("asset_cleanup_failed submission=%s", submission.id)

        try:
            for key, image in zip(keys, data, strict=True):
                runtime.assets.put(key, image)
                stored.append(key)
        except AssetUnavailable:
            clean_stored()
            raise
        try:
            with runtime.db.transaction() as session:
                session.add(submission)
                session.flush()
                job = Job(submission_id=submission.id)
                session.add(job)
                session.add_all(
                    Page(submission_id=submission.id, number=index, object_key=key)
                    for index, key in enumerate(keys, 1)
                )
                from edu_app.knowledge import create_discovery

                create_discovery(session, owner, "math", submission.id, [
                    {"number": index, "key": key} for index, key in enumerate(keys, 1)
                ])
                session.flush()
                return _submission_info(submission, job)
        except IntegrityError:
            clean_stored()
            with runtime.db.session() as session:
                if existing := session.execute(existing_query).one_or_none():
                    return _submission_info(*existing)
            raise

    @app.get("/api/submissions")
    def history(owner: str = require_owner):
        with runtime.db.session() as session:
            rows = session.execute(
                select(Submission, Job)
                .join(Job, Job.submission_id == Submission.id)
                .where(Submission.owner == owner)
                .order_by(Submission.created_at.desc())
                .limit(25)
            )
            return [_submission_info(submission, job) for submission, job in rows]

    @app.get("/api/submissions/{submission_id}")
    def submission_detail(submission_id: UUID, owner: str = require_owner):
        with runtime.db.session() as session:
            row = session.execute(
                select(Submission, Job)
                .join(Job, Job.submission_id == Submission.id)
                .where(Submission.id == str(submission_id), Submission.owner == owner)
            ).one_or_none()
            if row is None:
                raise HTTPException(404, _NOT_FOUND)
            submission, job = row
            pages = list(
                session.scalars(
                    select(Page.number).where(Page.submission_id == submission.id).order_by(Page.number)
                )
            )
            report = (
                session.get(Report, (submission.id, submission.revision)) if submission.revision else None
            )
            return {
                **_submission_info(submission, job, report if runtime.settings.math_review_retry_enabled else None),
                "pages": pages,
                "report": public_report(report.document) if report else None,
            }

    @app.get("/api/submissions/{submission_id}/pages/{number}")
    def view_page(submission_id: UUID, number: int, owner: str = require_owner):
        with runtime.db.session() as session:
            submission = _submission(session, owner, submission_id)
            page = session.get(Page, (submission.id, number))
            if not page:
                raise HTTPException(404, _NOT_FOUND)
        try:
            return Response(runtime.assets.get(page.object_key), media_type="image/jpeg")
        except AssetNotFound as exc:
            raise HTTPException(404, "原图已经找不到了，请重拍后上传。") from exc

    @app.post("/api/submissions/{submission_id}/retry", status_code=202)
    def retry_submission(submission_id: UUID, owner: str = require_owner):
        with runtime.db.transaction() as session:
            submission = _submission(session, owner, submission_id)
            job = session.scalars(
                select(Job).where(Job.submission_id == submission.id).with_for_update()
            ).one()
            session.refresh(submission, with_for_update=True)
            report = (
                session.get_one(Report, (submission.id, submission.revision))
                if submission.revision and runtime.settings.math_review_retry_enabled else None
            )
            if not _can_retry(submission, job, report):
                raise HTTPException(409, "此作业当前无法重试，请刷新查看最新状态。")
            job.status, job.error, job.attempts = "queued", None, 0
            job.available_at, job.dispatched_at = now_ms(), None
            job.manual_retries += 1
            return _submission_info(submission, job)

    @app.post("/api/submissions/{submission_id}/reanalyze", status_code=202)
    def reanalyze_submission(submission_id: UUID, owner: str = require_owner):
        with runtime.db.transaction() as session:
            submission = _submission(session, owner, submission_id)
            job = session.scalars(select(Job).where(Job.submission_id == submission.id).with_for_update()).one()
            session.refresh(submission, with_for_update=True)
            if not submission.revision or not can_reanalyze(job):
                raise HTTPException(409, "此作业当前无法重新分析，请刷新查看最新状态。")
            clear_page_checkpoints(session, submission.id)
            queue_reanalysis(job)
            return _submission_info(submission, job)

    @app.patch("/api/submissions/{submission_id}/items/{item_id}/transcription")
    def correct_transcription(
        submission_id: UUID, item_id: UUID, body: Correction, owner: str = require_owner
    ):
        with runtime.db.transaction() as session:
            submission = _submission(session, owner, submission_id, lock=True)
            if body.revision != submission.revision:
                raise HTTPException(409, "报告已更新，请刷新后再核对。")
            job = session.scalars(select(Job).where(Job.submission_id == submission.id)).one()
            if job.reanalysis and job.status in ("queued", "running"):
                raise HTTPException(409, "作业正在重新分析，请等新报告完成后再核对。")
            report = session.get_one(Report, (submission.id, submission.revision))
            for index, value in enumerate(report.document["items"]):
                if value["id"] != str(item_id):
                    continue
                item = ReportItem.model_validate(value)
                answer = body.answer.strip()
                grade = grade_item(item.analysis, correction=answer)
                if grade.score is None:
                    raise HTTPException(422, grade.feedback)
                current_answer = (item.correction or item.analysis.source.answer or "").strip()
                if item.score is not None and answer == current_answer:
                    return {"revision": submission.revision, "report": public_report(report.document)}
                corrected = ReportItem(
                    id=item.id,
                    page=item.page,
                    analysis=item.analysis,
                    correction=answer,
                    **ReportItem.from_grade(grade),
                )
                document = {**report.document, "items": report.document["items"].copy()}
                document["items"][index] = corrected.model_dump()
                save_report(session, submission, document)
                return {"revision": submission.revision, "report": public_report(document)}
            raise HTTPException(404, _NOT_FOUND)

    @app.get("/api/mastery")
    def mastery(rules_only: bool = False, owner: str = require_owner):
        with runtime.db.session() as session:
            return overview(session, owner, include_ai=not rules_only)

    @app.post("/api/practice-sets", status_code=202)
    def create_practice(body: CreatePractice, owner: str = require_owner):
        with runtime.db.transaction() as session:
            skills = overview(session, owner)
            current = next(skill for skill in skills if skill["id"] == body.skill)
            practice = Practice(
                owner=owner,
                skill=body.skill,
                difficulty=body.difficulty,
                count=body.count,
                purpose="diagnose" if current["status"] == "insufficient" else "strengthen",
            )
            session.add(practice)
            session.flush()
            job = Job(practice_id=practice.id)
            session.add(job)
            session.flush()
            return _practice_info(practice, job)

    @app.get("/api/practice-sets")
    def practice_history(owner: str = require_owner):
        with runtime.db.session() as session:
            rows = session.execute(
                select(Practice, Job)
                .join(Job, Job.practice_id == Practice.id)
                .where(Practice.owner == owner)
                .order_by(Practice.created_at.desc())
                .limit(25)
            )
            return [_practice_info(practice, job) for practice, job in rows]

    @app.get("/api/practice-sets/{practice_id}")
    def practice_detail(practice_id: UUID, owner: str = require_owner):
        with runtime.db.session() as session:
            row = session.execute(
                select(Practice, Job)
                .join(Job, Job.practice_id == Practice.id)
                .where(Practice.owner == owner, Practice.id == str(practice_id))
            ).one_or_none()
            if not row:
                raise HTTPException(404, _NOT_FOUND)
            practice, job = row
            items = session.scalars(
                select(PracticeItem)
                .where(PracticeItem.practice_id == practice.id)
                .order_by(PracticeItem.position)
            )
            return {**_practice_info(practice, job), "items": [_public_practice_item(item) for item in items]}

    @app.post("/api/practice-sets/{practice_id}/items/{item_id}/hint")
    def hint(practice_id: UUID, item_id: UUID, owner: str = require_owner):
        with runtime.db.transaction() as session:
            item, practice = _practice_item(session, owner, practice_id, item_id)
            if item.score is None:
                item.hint_seen = True
            return {"hint": SKILL_BY_ID[practice.skill].hint}

    @app.post("/api/practice-sets/{practice_id}/items/{item_id}/answer")
    def answer_practice(practice_id: UUID, item_id: UUID, body: Answer, owner: str = require_owner):
        with runtime.db.transaction() as session:
            item, practice = _practice_item(session, owner, practice_id, item_id)
            answer = body.answer.strip()
            if item.score is not None:
                if answer == item.answer:
                    return _answer_result(item)
                raise HTTPException(409, "这道题已经提交过，刷新即可查看结果。")
            try:
                verified = verify(ProblemSpec.model_validate(item.spec))
                if verified.expected != item.expected or verified.key != item.problem_key:
                    raise HTTPException(409, "题目规则已更新，请生成新的练习。")
                correct = check_answer(verified, answer)
            except MathError as exc:
                raise HTTPException(422, "作答格式无法核验，请检查运算符，或选择一个选项。") from exc
            item.answer = answer
            item.score = 2 if correct else 0
            item.answered_at = now_ms()
            item.counted = not item.hint_seen
            if item.counted:
                session.add(
                    Evidence(
                        source_id=item.id,
                        owner=owner,
                        skill=practice.skill,
                        problem_key=item.problem_key,
                        score=item.score,
                        occurred_at=item.answered_at,
                        practice_id=practice.id,
                    )
                )
            return _answer_result(item)

    @app.get("/")
    def web():
        if not (settings.web_dir / "index.html").is_file():
            raise HTTPException(503, "前端尚未构建，请按 README 启动。")
        return FileResponse(settings.web_dir / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/auth-redirect.html")
    def auth_redirect():
        return FileResponse(settings.web_dir / "auth-redirect.html", headers={"Cache-Control": "no-store"})

    if (settings.web_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.web_dir / "assets"), name="web-assets")
    return app
