import logging
import re
from dataclasses import dataclass
from typing import Literal

from edu_domain.contracts import AnalyzedItem, Calculation, Outcome
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
from edu_domain.symbolic import CalculationError, calculate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Grade:
    outcome: Outcome
    feedback: str
    problem: Verified | None = None
    score: int | None = None
    reference: str | None = None
    verification: Literal["rules", "ai_review", "unresolved"] = "rules"
    reason: str | None = None
    tool_checked: bool = False


def _failed(reason: str, feedback: str) -> Grade:
    logger.info("math_verification_failed code=%s", reason)
    return Grade("undetermined", feedback, verification="unresolved", reason=reason)


def _compare(first: str, second: str, variables: list[str]) -> bool | None:
    allowed = set(variables) | {"Abs", "abs", "sqrt", "binomial", "factorial", "pi", "E", "I",
                                "sin", "cos", "tan", "exp", "log"}
    if any(
        len(text) > 2000 or not re.fullmatch(r"[A-Za-z0-9+\-*/^().\s|]+", text)
        or set(re.findall(r"[A-Za-z][A-Za-z0-9]*", text)) - allowed
        for text in (first, second)
    ):
        return None
    result = calculate(Calculation(
        operation="equivalent", expression=first, variables=variables, bindings=[], comparison=second,
    ))
    if result.status != "ok" or result.value not in ("true", "false"):
        raise CalculationError(result.reason or "calculation_invalid_result")
    return result.value == "true"


def _grade_open(item: AnalyzedItem, correction: str | None, *, ai_only: bool) -> Grade:
    assessment = item.assessment
    if not item.source.readable or item.source.question is None or assessment is None:
        return _failed("incomplete_question", "没有完整、可靠的题目转写，暂不判分。请核对原图。")
    if correction is not None:
        return _failed("review_required", "此题需要重新进行 AI 复核，不能直接复用原判分修改作答。")
    review = assessment.review
    if not assessment.first.index == assessment.second.index == review.index:
        return _failed("invalid_review", "解题与复核结果无法对应同一道题，暂不判分。")
    if not review.source_consistent:
        return _failed("source_mismatch", "题目、图形、条件或作答转写与原图不一致，请核对原图。")
    if not review.unambiguous or any(
        not solution.answer or not solution.answer.strip() or not solution.explanation.strip()
        for solution in (assessment.first, assessment.second)
    ):
        return _failed("missing_information", "题目存在歧义、缺少条件，或独立解题未得到完整结果。")
    if not review.references_agree:
        return _failed("model_disagreement", "两次独立解题结论不一致，暂不判分。")
    checked = False
    variables = []
    if not ai_only:
        variables = list(dict.fromkeys(
            variable for solution in (assessment.first, assessment.second) if solution.calculation
            for variable in solution.calculation.variables
        ))
        if len(variables) > 16:
            return _failed("tool_limit", "解题所需计算超过本次资源预算，暂不判分。")
        if _compare(assessment.first.answer, assessment.second.answer, variables) is False:
            return _failed("model_disagreement", "独立解题的参考答案经计算比较不等价，暂不判分。")
        for solution, recorded in (
            (assessment.first, assessment.first_tool), (assessment.second, assessment.second_tool),
        ):
            if solution.calculation is None:
                if recorded is not None:
                    return _failed("invalid_tool_evidence", "计算证据与解题请求不一致，暂不判分。")
                continue
            actual = calculate(solution.calculation)
            if actual.status != "ok" or actual.value is None:
                return _failed("tool_failure", "计算工具未能完成核验（格式、资源或运行问题），暂不判分。")
            if recorded != actual:
                return _failed("invalid_tool_evidence", "计算结果无法复现，暂不判分。")
            checked = True
            if not item.source.choices and solution.calculation_role == "answer" and (
                solution.calculation.operation in ("evaluate", "simplify")
            ):
                if _compare(solution.answer, actual.value, solution.calculation.variables) is False:
                    return _failed("model_disagreement", "参考答案与计算工具的结果不一致，暂不判分。")
    source = item.source
    if source.mark == "blank":
        if source.answer or source.steps or review.outcome != "blank":
            return _failed("mark_mismatch", "留白标记与可见作答不一致，请核对原图。")
        return Grade("blank", "这题没有可见作答，不计入掌握度。", verification="ai_review",
                     tool_checked=checked)
    if source.mark == "unclear" or not source.answer or not source.answer.strip():
        return _failed("answer_unclear", "学生作答无法可靠识别，请核对或重拍答题区域。")
    if review.outcome not in ("correct", "partial", "incorrect"):
        return _failed("review_inconclusive", review.feedback)
    if review.outcome == "partial" and not review.steps:
        return _failed("invalid_partial", "部分正确的判定缺少具体步骤依据，暂不判分。")
    if not ai_only and review.outcome in ("correct", "incorrect") and not source.choices:
        equivalent = _compare(source.answer, assessment.first.answer, variables)
        if (review.outcome == "correct" and equivalent is False) or (
            review.outcome == "incorrect" and equivalent is True
        ):
            return _failed("review_disagreement", "AI 判分与数学等价性检查冲突，暂不判分。")
    return Grade(
        review.outcome, review.feedback, score={"correct": 2, "partial": 1, "incorrect": 0}[review.outcome],
        reference=assessment.first.answer, verification="ai_review", tool_checked=checked,
    )


def _verify_item(item: AnalyzedItem) -> Verified:
    source, spec = item.source, item.spec
    if not source.readable or not source.expression or not spec or not item.candidate:
        raise MathError("Insufficient source or solution")
    if source.question and (
        source.question.conditions or len(source.question.parts) > 1 or source.question.diagram
        or not matches_printed_target(spec.task, source.question.text, source.expression)
    ):
        raise MathError("The complete question differs from the single printed target")
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


def grade_item(item: AnalyzedItem, *, correction: str | None = None, ai_only: bool = False) -> Grade:
    if item.review_timed_out:
        return _failed("review_timeout", "本次独立解题或复核超时，暂不判分，也不计入掌握度。")
    if item.assessment is not None:
        try:
            return _grade_open(item, correction, ai_only=ai_only)
        except CalculationError:
            return _failed("tool_failure", "数学等价性检查未能完成，暂不判分。")
    if not item.source.readable:
        return _failed("ambiguous_image", "题目区域不完整或识别存在歧义，请核对原图。")
    if ai_only:
        return _failed("missing_review", "开放题型的独立解题或复核结果不完整，暂不判分。")
    try:
        problem = _verify_item(item)
    except MathError as exc:
        if item.source.question is not None:
            return _failed("missing_review", "开放题型的独立解题或复核结果不完整，暂不判分。")
        message = str(exc)
        if "differs" in message or "printed target" in message:
            return _failed("source_mismatch", "解题采用的表达式或目标与转写题干不一致，请核对原图。")
        if "disagree" in message:
            return _failed("model_disagreement", "模型解答与确定性核验器结论不一致，暂不判分。")
        return _failed("verification_failed", "尚未获得可靠的解题核验结果，暂不判分。")

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
