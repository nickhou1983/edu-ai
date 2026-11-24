from dataclasses import dataclass

from edu_domain.contracts import AnalyzedItem, Outcome
from edu_domain.curriculum import SKILL_BY_ID
from edu_domain.math import (
    MathError,
    Verified,
    check_answer,
    expression_signature,
    has_verified_step,
    matches_printed_target,
    verify,
)


@dataclass(frozen=True)
class Grade:
    outcome: Outcome
    feedback: str
    problem: Verified | None = None
    score: int | None = None


def _verify_item(item: AnalyzedItem) -> Verified:
    source, spec = item.source, item.spec
    if not source.readable or not source.expression or not spec or not item.candidate:
        raise MathError("Insufficient source or solution")
    if source.task != spec.task or len(source.choices) != len(spec.choices):
        raise MathError("Model solution differs from transcription")
    if expression_signature(spec.task, source.expression) != expression_signature(spec.task, spec.expression):
        raise MathError("Model expression differs from transcription")
    if not matches_printed_target(spec.task, source.stem, source.expression):
        raise MathError("The transcribed expression is not the single printed target")
    if any(
        expression_signature(
            spec.task if spec.task == "solve" and "=" in first and "=" in second else "evaluate", first
        )
        != expression_signature(
            spec.task if spec.task == "solve" and "=" in first and "=" in second else "evaluate", second
        )
        for first, second in zip(source.choices, spec.choices, strict=True)
    ):
        raise MathError("Model choices differ from transcription")
    problem = verify(spec)
    if not check_answer(problem, item.candidate):
        raise MathError("Model and verifier disagree")
    return problem


def grade_item(item: AnalyzedItem, *, correction: str | None = None) -> Grade:
    try:
        problem = _verify_item(item)
    except MathError:
        return Grade("undetermined", "题目识别或解答暂无法核验。请检查题图是否完整、清晰。")

    source = item.source
    if correction is None and source.mark == "blank":
        if source.answer is not None or source.steps:
            return Grade("undetermined", "识别到的作答与留白标记不一致，请核对原图。", problem)
        return Grade("blank", "这题没有可见作答，不计入掌握度。", problem)
    if correction is None and (source.mark == "unclear" or not source.answer):
        return Grade("undetermined", "看不清学生的最终作答。你可以重拍，或核对转写查看参考结果。", problem)
    answer = correction if correction is not None else source.answer
    if not answer:
        return Grade("undetermined", "没有可核验的作答。", problem)

    try:
        correct = check_answer(problem, answer)
    except MathError:
        return Grade("undetermined", "作答格式无法核验。请核对原图和转写。", problem)
    if correct:
        return Grade("correct", problem.explanation, problem, 2)
    if correction is None and has_verified_step(problem, source.steps):
        return Grade(
            "partial", f"有可核验的等价方程步骤，但最终结果不正确。{problem.explanation}", problem, 1
        )
    return Grade("incorrect", f"{problem.explanation} {SKILL_BY_ID[problem.skill].hint}", problem, 0)
