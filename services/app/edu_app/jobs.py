import logging
from base64 import b64encode
from collections.abc import Iterator
from dataclasses import dataclass
from random import randint
from typing import cast

from pydantic import ValidationError
from sqlalchemy import and_, null, or_, select, update
from sqlalchemy.orm import Session

from edu_agent.demo import DemoImageOnly
from edu_app.agent_client import Agent, AgentFailure, agent_job_id
from edu_app.assets import AssetNotFound, AssetUnavailable
from edu_app.db import (
    EnglishRecord,
    Job,
    KnowledgeActivity,
    Page,
    Practice,
    PracticeItem,
    Report,
    Submission,
    new_id,
    now_ms,
)
from edu_app.english_jobs import english_work, save_english
from edu_app.knowledge_jobs import knowledge_work, save_knowledge
from edu_app.reports import ReportItem, is_pending_review, make_report, page_version, save_report
from edu_app.runtime import Runtime
from edu_domain.agent_protocol import MAX_AGENT_METADATA_BYTES
from edu_domain.contracts import (
    MAX_AI_REVIEW_BATCH,
    RULES_VERSION,
    SCHEMA_VERSION,
    AgentVersion,
    AnalysisResult,
    AnalyzedItem,
    AnalyzePage,
    Difficulty,
    GeneratePractice,
    PracticeResult,
    Purpose,
    SkillId,
)
from edu_domain.grading import grade_item
from edu_domain.math import MathError, Verified, verify

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
LEASE_MS = 15 * 60_000
WORKER_TIMEOUT_SECONDS = 2 * 60 * 60
JOB_ERRORS = {
    "agent_unavailable": "分析服务暂时不可用，请稍后重试。",
    "agent_timeout": "分析服务响应超时，请稍后重试。",
    "agent_rate_limited": "请求较多，请稍后重试。",
    "agent_invalid_result": "分析结果无法读取，请稍后重试。",
    "agent_no_result": "分析服务无法处理这次请求。请裁掉无关区域，或换一张清晰的图片。",
    "asset_unavailable": "暂时无法读取原图，请稍后重试。",
    "image_missing": "原图已经找不到了，请重拍后上传。",
    "demo_image_only": "本地演示只能分析本站的合成样卷，未对这张图片判分。",
    "review_unavailable": "当前分析服务不支持重试复核。",
    "practice_unfilled": "暂时凑不齐通过验证的练习题，请重新生成。",
    "interrupted": "任务多次中断，请稍后重试。",
}
RETRYABLE_FAILURES = {
    "agent_unavailable", "agent_timeout", "agent_rate_limited", "agent_invalid_result",
    "asset_unavailable", "interrupted",
}


@dataclass(frozen=True)
class Work:
    id: str
    token: str
    submission_id: str | None
    practice_id: str | None
    english_id: str | None
    knowledge_id: str | None


@dataclass(frozen=True)
class PracticeBatch:
    problems: list[Verified]
    versions: list[AgentVersion]


def _eligible(timestamp: int):
    return or_(
        and_(Job.status == "queued", Job.available_at <= timestamp),
        and_(Job.status == "running", Job.lease_until <= timestamp),
    )


def _claim(runtime: Runtime, job_id: str | None) -> Work | None:
    timestamp = now_ms()
    with runtime.db.transaction() as session:
        query = select(Job).where(_eligible(timestamp)).order_by(
            Job.knowledge_id.is_not(None), Job.available_at, Job.id
        ).limit(1)
        if job_id is not None:
            query = query.where(Job.id == job_id)
        job = session.scalar(query.with_for_update(skip_locked=True))
        if not job:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.error = "failed", "interrupted"
            job.lease_token = job.lease_until = None
            if job.submission_id and job.manual_retries:
                _clear_page_checkpoints(session, job.submission_id)
            logger.warning("job_abandoned id=%s", job.id)
            return None
        job.status, job.lease_token, job.lease_until = "running", new_id(), timestamp + LEASE_MS
        job.attempts += 1
        job.error = None
        return Work(job.id, job.lease_token, job.submission_id, job.practice_id, job.english_id, job.knowledge_id)


def _analyze(runtime: Runtime, agent: Agent, work: Work) -> dict:
    from edu_app.knowledge import catalog

    assert work.submission_id is not None
    submission_id = work.submission_id
    with runtime.db.session() as session:
        ai_only = session.get_one(Submission, submission_id).ai_mastery
        pages = list(
            session.execute(
                select(Page.number, Page.object_key, Page.analysis_checkpoint)
                .where(Page.submission_id == submission_id).order_by(Page.number)
            )
        )
        points = catalog(session, "math")
    known = {point.id for point in points}
    results = []
    for number, object_key, checkpoint in pages:
        _renew(runtime, work)
        if checkpoint is not None:
            try:
                cached = AnalysisResult.model_validate(checkpoint)
            except ValidationError:
                logger.warning("page_checkpoint_invalid id=%s page=%d", work.id, number)
            else:
                if _valid_page(cached, known, ai_only=ai_only):
                    results.append((number, cached))
                    continue
                logger.info("page_checkpoint_stale id=%s page=%d", work.id, number)
        try:
            result = agent.invoke(
                AnalyzePage(
                    version="2",
                    operation="analyze_submission",
                    image_base64=_page_image(runtime, object_key),
                    catalog=points,
                    ai_only=ai_only,
                )
            )
        except DemoImageOnly as exc:
            raise AgentFailure("demo_image_only") from exc
        if not isinstance(result, AnalysisResult) or not _valid_page(result, known, ai_only=ai_only):
            raise AgentFailure("agent_invalid_result", retryable=True)
        with runtime.db.transaction() as session:
            job = _require_lease(session, work)
            page = session.get_one(Page, (submission_id, number))
            page.analysis_checkpoint = result.model_dump(mode="json")
            job.lease_until = now_ms() + LEASE_MS
        results.append((number, result))
    return make_report(submission_id, results, before_item=lambda: _renew(runtime, work))


def _valid_page(result: AnalysisResult, known: set[str], *, ai_only: bool) -> bool:
    if ai_only and (result.version != "2" or any(
        item.spec is not None or item.candidate is not None
        or (item.source.readable and item.source.question is None)
        or (item.assessment is not None and item.assessment.first.index != index)
        for index, item in enumerate(result.items)
    )):
        return False
    return all(
        candidate.existing_id is None or candidate.existing_id in known
        for item in result.items if item.source.question
        for candidate in item.source.question.points
    )


def _page_image(runtime: Runtime, object_key: str) -> str:
    try:
        return b64encode(runtime.assets.get(object_key)).decode("ascii")
    except AssetNotFound as exc:
        raise AgentFailure("image_missing") from exc
    except AssetUnavailable as exc:
        raise AgentFailure("asset_unavailable", retryable=True) from exc


def _review_batches(items: list[ReportItem], *, ai_only: bool = False) -> Iterator[list[ReportItem]]:
    # Leave room for the request envelope and delimiters within the Host's body limit.
    budget = MAX_AGENT_METADATA_BYTES - 1024
    batch = []
    size = 0
    for item in items:
        item_size = len(item.analysis.source.model_dump_json().encode("utf-8"))
        if item_size > budget:
            raise AgentFailure("agent_invalid_result")
        if batch and (size + item_size > budget or (ai_only and len(batch) == MAX_AI_REVIEW_BATCH)):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch


def _review_batch(
    runtime: Runtime, agent: Agent, work: Work, image: str, targets: list[ReportItem],
    *, ai_only: bool,
) -> tuple[AnalysisResult, dict[str, ReportItem]]:
    request = AnalyzePage(
        version="2", operation="analyze_submission", image_base64=image,
        ai_only=ai_only,
        review_items=[target.analysis.source for target in targets],
    )
    try:
        result = agent.invoke(request)
    except DemoImageOnly as exc:
        raise AgentFailure("review_unavailable") from exc
    if not isinstance(result, AnalysisResult) or result.version != "2" or result.quality != "readable" or (
        len(result.items) != len(targets)
    ):
        raise AgentFailure("agent_invalid_result", retryable=True)

    replacements = {}
    for index, (target, reviewed) in enumerate(zip(targets, result.items, strict=True)):
        if reviewed.source != target.analysis.source or reviewed.spec is not None or reviewed.candidate is not None:
            raise AgentFailure("agent_invalid_result", retryable=True)
        if reviewed.review_timed_out:
            raise AgentFailure("agent_timeout", retryable=True)
        assessment = reviewed.assessment
        if assessment is None:
            continue
        if not index == assessment.first.index == assessment.second.index == assessment.review.index:
            raise AgentFailure("agent_invalid_result", retryable=True)
        _renew(runtime, work)
        analysis = AnalyzedItem(source=target.analysis.source, spec=None, candidate=None, assessment=assessment)
        replacements[target.id] = ReportItem(
            id=target.id, page=target.page, analysis=analysis, **ReportItem.from_grade(grade_item(analysis)),
        )
    return result, replacements


def _save_review_batch(
    runtime: Runtime, work: Work, submission_id: str, number: int,
    result: AnalysisResult, replacements: dict[str, ReportItem],
) -> None:
    with runtime.db.transaction() as session:
        job = _require_lease(session, work)
        submission = session.get_one(Submission, submission_id, with_for_update=True)
        report = session.get_one(Report, (submission_id, submission.revision))
        items = []
        changed = False
        for value in report.document["items"]:
            replacement = replacements.get(value["id"])
            if replacement and is_pending_review(value):
                current = ReportItem.model_validate(value)
                if current.page != replacement.page or current.analysis.source != replacement.analysis.source:
                    raise AgentFailure("agent_invalid_result", retryable=True)
                value = replacement.model_dump()
                changed = True
            items.append(value.copy())
        if changed:
            document = {
                **report.document,
                "items": items,
                "versions": [*report.document["versions"], page_version(number, result)],
            }
            save_report(session, submission, document)
        job.lease_until = now_ms() + LEASE_MS


def _retry_pending_reviews(runtime: Runtime, agent: Agent, work: Work, submission_id: str) -> bool:
    with runtime.db.session() as session:
        submission = session.get_one(Submission, submission_id)
        if not submission.revision:
            return False
        ai_only = submission.ai_mastery
        report = session.get_one(Report, (submission_id, submission.revision))
        targets: dict[int, list[ReportItem]] = {}
        for value in report.document["items"]:
            if is_pending_review(value):
                item = ReportItem.model_validate(value)
                targets.setdefault(item.page, []).append(item)
        page_keys = {
            number: key for number, key in session.execute(
                select(Page.number, Page.object_key).where(Page.submission_id == submission_id)
            )
        }

    for number, items in targets.items():
        _renew(runtime, work)
        if number not in page_keys:
            raise AgentFailure("image_missing")
        image = _page_image(runtime, page_keys[number])
        for batch in _review_batches(items, ai_only=ai_only):
            _renew(runtime, work)
            result, replacements = _review_batch(runtime, agent, work, image, batch, ai_only=ai_only)
            if replacements:
                _save_review_batch(runtime, work, submission_id, number, result, replacements)
            if len(replacements) < len(batch):
                raise AgentFailure("agent_invalid_result", retryable=True)

    with runtime.db.transaction() as session:
        _complete(_require_lease(session, work))
    return True


def _practice(runtime: Runtime, agent: Agent, practice_id: str) -> PracticeBatch:
    with runtime.db.session() as session:
        practice = session.get_one(Practice, practice_id)
        request = GeneratePractice(
            version=SCHEMA_VERSION,
            operation="generate_practice",
            skill=cast(SkillId, practice.skill),
            purpose=cast(Purpose, practice.purpose),
            difficulty=cast(Difficulty, practice.difficulty),
            count=practice.count,
        )
    valid: dict[str, Verified] = {}
    versions = []
    for _ in range(3):
        result = agent.invoke(request)
        if not isinstance(result, PracticeResult):
            raise AgentFailure("agent_invalid_result", retryable=True)
        versions.append(result.provenance)
        for spec in result.items:
            try:
                problem = verify(spec)
            except MathError:
                continue
            if problem.skill == request.skill and problem.difficulty == request.difficulty:
                valid.setdefault(problem.key, problem)
            if len(valid) == request.count:
                return PracticeBatch(list(valid.values()), versions)
    raise AgentFailure("practice_unfilled")


def _lease(session: Session, work: Work) -> Job | None:
    return session.scalar(
        select(Job)
        .where(
            Job.id == work.id,
            Job.lease_token == work.token,
            Job.status == "running",
        )
        .with_for_update()
    )


def _clear_page_checkpoints(session: Session, submission_id: str) -> None:
    session.execute(
        update(Page).where(Page.submission_id == submission_id, Page.analysis_checkpoint.is_not(None))
        .values(analysis_checkpoint=null())
    )


def _fail(runtime: Runtime, work: Work, failure: AgentFailure, *, invalidate_checkpoints: bool = False):
    with runtime.db.transaction() as session:
        job = _lease(session, work)
        if not job:
            return
        job.error = failure.code
        job.lease_token = job.lease_until = None
        retry = failure.retryable and job.attempts < MAX_ATTEMPTS
        job.status = "queued" if retry else "failed"
        delay_ms = None
        if retry:
            delay_ms = max(15_000 * (2 ** (job.attempts - 1)), (failure.retry_after or 0) * 1000)
            delay_ms = min(delay_ms + randint(0, 5_000), 120_000)
            job.available_at = now_ms() + delay_ms
            job.dispatched_at = None
        if work.submission_id and (
            invalidate_checkpoints or (not retry and (job.manual_retries or job.error not in RETRYABLE_FAILURES))
        ):
            _clear_page_checkpoints(session, work.submission_id)
        logger.warning("job_attempt_failed id=%s code=%s retry=%s attempt=%d retry_in_ms=%s",
                       job.id, failure.code, retry, job.attempts, delay_ms)


def _require_lease(session: Session, work: Work) -> Job:
    job = _lease(session, work)
    if not job:
        logger.warning("job_lease_lost id=%s", work.id)
        raise AgentFailure("interrupted")
    return job


def _renew(runtime: Runtime, work: Work) -> None:
    with runtime.db.transaction() as session:
        _require_lease(session, work).lease_until = now_ms() + LEASE_MS


def run_one(runtime: Runtime, agent: Agent, job_id: str | None = None) -> bool:
    work = _claim(runtime, job_id)
    if not work:
        return False
    token = agent_job_id.set(work.id)
    try:
        return _run_work(runtime, agent, work)
    finally:
        agent_job_id.reset(token)


def _run_work(runtime: Runtime, agent: Agent, work: Work) -> bool:
    if work.knowledge_id:
        try:
            document = knowledge_work(runtime, agent, work.knowledge_id)
            with runtime.db.transaction() as session:
                job = _lease(session, work)
                if not job:
                    return True
                activity = session.get_one(KnowledgeActivity, work.knowledge_id, with_for_update=True)
                save_knowledge(session, activity, document)
                _complete(job)
        except AgentFailure as exc:
            _fail(runtime, work, exc)
            return True
    elif work.english_id:
        try:
            document = english_work(runtime, agent, work.english_id)
        except AgentFailure as exc:
            _fail(runtime, work, exc)
            return True
        with runtime.db.transaction() as session:
            job = _lease(session, work)
            if not job:
                return True
            record = session.get_one(EnglishRecord, work.english_id, with_for_update=True)
            save_english(session, record, document)
            _complete(job)
    elif work.submission_id:
        try:
            if not _retry_pending_reviews(runtime, agent, work, work.submission_id):
                report = _analyze(runtime, agent, work)
                with runtime.db.transaction() as session:
                    job = _lease(session, work)
                    if not job:
                        return True
                    submission = session.get_one(Submission, work.submission_id, with_for_update=True)
                    save_report(session, submission, report)
                    _clear_page_checkpoints(session, submission.id)
                    # The initial analysis and the finished report each allow one manual retry.
                    job.manual_retries = 0
                    _complete(job)
        except AgentFailure as exc:
            _fail(runtime, work, exc)
            return True
        except ValueError:
            _fail(runtime, work, AgentFailure("agent_invalid_result", retryable=True), invalidate_checkpoints=True)
            return True
    else:
        assert work.practice_id is not None
        try:
            batch = _practice(runtime, agent, work.practice_id)
        except AgentFailure as exc:
            _fail(runtime, work, exc)
            return True
        with runtime.db.transaction() as session:
            job = _lease(session, work)
            if not job:
                return True
            practice = session.get_one(Practice, work.practice_id, with_for_update=True)
            practice.provenance = [
                {**version.model_dump(), "schema": SCHEMA_VERSION, "rules": RULES_VERSION}
                for version in batch.versions
            ]
            for position, problem in enumerate(batch.problems, 1):
                session.add(
                    PracticeItem(
                        practice_id=practice.id,
                        position=position,
                        problem_key=problem.key,
                        spec=problem.spec.model_dump(),
                        expected=problem.expected,
                        explanation=problem.explanation,
                        rules_version=RULES_VERSION,
                    )
                )
            _complete(job)
    logger.info("job_completed id=%s", work.id)
    return True


def _complete(job: Job):
    job.status, job.error = "done", None
    job.lease_token = job.lease_until = None
