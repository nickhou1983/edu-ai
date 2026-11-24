import logging
from base64 import b64encode
from dataclasses import dataclass
from typing import cast

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from edu_agent.demo import DemoImageOnly
from edu_app.agent_client import Agent, AgentFailure
from edu_app.assets import AssetNotFound, AssetUnavailable
from edu_app.db import Job, Page, Practice, PracticeItem, Submission, new_id, now_ms
from edu_app.reports import make_report, save_report
from edu_app.runtime import Runtime
from edu_domain.contracts import (
    RULES_VERSION,
    SCHEMA_VERSION,
    AgentVersion,
    AnalysisResult,
    AnalyzePage,
    Difficulty,
    GeneratePractice,
    PracticeResult,
    Purpose,
    SkillId,
)
from edu_domain.math import MathError, Verified, verify

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
LEASE_MS = 15 * 60_000
JOB_ERRORS = {
    "agent_unavailable": "分析服务暂时不可用，请稍后重试。",
    "agent_invalid_result": "分析结果无法读取，请稍后重试。",
    "agent_no_result": "分析服务无法处理这次请求。请裁掉无关区域，或换一张清晰的图片。",
    "asset_unavailable": "暂时无法读取原图，请稍后重试。",
    "image_missing": "原图已经找不到了，请重拍后上传。",
    "demo_image_only": "本地演示只能分析本站的合成样卷，未对这张图片判分。",
    "practice_unfilled": "暂时凑不齐通过验证的练习题，请重新生成。",
    "interrupted": "任务多次中断，请稍后重试。",
}
RETRYABLE_FAILURES = {"agent_unavailable", "agent_invalid_result", "asset_unavailable", "interrupted"}


@dataclass(frozen=True)
class Work:
    id: str
    token: str
    submission_id: str | None
    practice_id: str | None


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
        query = select(Job).where(_eligible(timestamp)).order_by(Job.available_at, Job.id).limit(1)
        if job_id is not None:
            query = query.where(Job.id == job_id)
        job = session.scalar(query.with_for_update(skip_locked=True))
        if not job:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.error = "failed", "interrupted"
            job.lease_token = job.lease_until = None
            logger.warning("job_abandoned id=%s", job.id)
            return None
        job.status, job.lease_token, job.lease_until = "running", new_id(), timestamp + LEASE_MS
        job.attempts += 1
        job.error = None
        return Work(job.id, job.lease_token, job.submission_id, job.practice_id)


def _analyze(runtime: Runtime, agent: Agent, submission_id: str) -> dict:
    with runtime.db.session() as session:
        pages = list(
            session.scalars(select(Page).where(Page.submission_id == submission_id).order_by(Page.number))
        )
    results = []
    for page in pages:
        try:
            data = runtime.assets.get(page.object_key)
        except AssetNotFound as exc:
            raise AgentFailure("image_missing") from exc
        except AssetUnavailable as exc:
            raise AgentFailure("asset_unavailable", retryable=True) from exc
        try:
            result = agent.invoke(
                AnalyzePage(
                    version=SCHEMA_VERSION,
                    operation="analyze_submission",
                    image_base64=b64encode(data).decode("ascii"),
                )
            )
        except DemoImageOnly as exc:
            raise AgentFailure("demo_image_only") from exc
        if not isinstance(result, AnalysisResult):
            raise AgentFailure("agent_invalid_result", retryable=True)
        results.append((page.number, result))
    return make_report(submission_id, results)


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


def _fail(runtime: Runtime, work: Work, failure: AgentFailure):
    with runtime.db.transaction() as session:
        job = _lease(session, work)
        if not job:
            return
        job.error = failure.code
        job.lease_token = job.lease_until = None
        retry = failure.retryable and job.attempts < MAX_ATTEMPTS
        job.status = "queued" if retry else "failed"
        if retry:
            job.available_at = now_ms() + job.attempts * 3_000
            job.dispatched_at = None
        logger.warning("job_attempt_failed id=%s code=%s retry=%s", job.id, failure.code, retry)


def run_one(runtime: Runtime, agent: Agent, job_id: str | None = None) -> bool:
    work = _claim(runtime, job_id)
    if not work:
        return False
    if work.submission_id:
        try:
            report = _analyze(runtime, agent, work.submission_id)
        except AgentFailure as exc:
            _fail(runtime, work, exc)
            return True
        with runtime.db.transaction() as session:
            job = _lease(session, work)
            if not job:
                return True
            submission = session.get_one(Submission, work.submission_id, with_for_update=True)
            save_report(session, submission, report)
            _complete(job)
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
