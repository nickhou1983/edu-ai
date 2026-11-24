from uuid import UUID, uuid5

from sqlalchemy import delete
from sqlalchemy.orm import Session

from edu_app.db import Evidence, Report, Submission
from edu_domain.contracts import (
    RULES_VERSION,
    SCHEMA_VERSION,
    AnalysisResult,
    AnalyzedItem,
    Contract,
    Outcome,
    SkillId,
)
from edu_domain.grading import Grade, grade_item
from edu_domain.math import problem_text


class ReportItem(Contract):
    id: str
    page: int
    analysis: AnalyzedItem
    outcome: Outcome
    feedback: str
    skill: SkillId | None
    problem_key: str | None
    score: int | None
    expected: str | None
    rules_version: str
    correction: str | None = None

    @staticmethod
    def from_grade(grade: Grade) -> dict:
        problem = grade.problem
        return {
            "outcome": grade.outcome,
            "feedback": grade.feedback,
            "skill": problem.skill if problem else None,
            "problem_key": problem.key if problem else None,
            "score": grade.score,
            "expected": problem.expected if problem and grade.score is not None else None,
            "rules_version": RULES_VERSION,
        }

    def public(self) -> dict:
        source = self.analysis.source
        spec = self.analysis.spec if self.problem_key else None
        return {
            "id": self.id,
            "page": self.page,
            "label": source.label,
            "region": source.region.model_dump(),
            "stem": source.stem or (problem_text(spec) if spec else ""),
            "choices": source.choices,
            "answer": self.correction if self.correction is not None else source.answer,
            "corrected": self.correction is not None,
            "expected": self.expected,
            "skill": self.skill,
            "outcome": self.outcome,
            "feedback": self.feedback,
            "canCorrect": spec is not None,
        }


def make_report(submission_id: str, pages: list[tuple[int, AnalysisResult]]) -> dict:
    items = []
    retake = []
    for number, result in pages:
        if result.quality == "unclear" or not result.items:
            retake.append(number)
            continue
        for index, item in enumerate(result.items):
            items.append(
                ReportItem(
                    id=str(uuid5(UUID(submission_id), f"{number}:{index}")),
                    page=number,
                    analysis=item,
                    **ReportItem.from_grade(grade_item(item)),
                ).model_dump()
            )
    return {
        "items": items,
        "retake_pages": retake,
        "versions": [
            {
                "page": number,
                **result.provenance.model_dump(),
                "schema": SCHEMA_VERSION,
                "rules": RULES_VERSION,
            }
            for number, result in pages
        ],
    }


def save_report(session: Session, submission: Submission, document: dict) -> None:
    submission.revision += 1
    # An edit replaces this submission's evidence without changing its original date.
    session.execute(delete(Evidence).where(Evidence.submission_id == submission.id))
    for value in document["items"]:
        item = ReportItem.model_validate(value)
        if item.correction is not None:
            continue
        if item.score is not None and item.problem_key and item.skill:
            session.add(
                Evidence(
                    source_id=item.id,
                    owner=submission.owner,
                    submission_id=submission.id,
                    skill=item.skill,
                    problem_key=item.problem_key,
                    score=item.score,
                    occurred_at=submission.created_at,
                )
            )
    session.add(Report(submission_id=submission.id, revision=submission.revision, document=document))


def public_report(document: dict) -> dict:
    items = [ReportItem.model_validate(item).public() for item in document["items"]]
    outcomes: tuple[Outcome, ...] = ("correct", "partial", "incorrect", "blank", "undetermined")
    counts = {outcome: sum(item["outcome"] == outcome for item in items) for outcome in outcomes}
    return {"items": items, "counts": counts, "retakePages": document["retake_pages"]}
