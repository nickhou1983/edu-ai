import json
import unicodedata
from dataclasses import dataclass
from hashlib import sha256

from edu_domain.contracts import (
    EnglishAssessment,
    EnglishProblem,
    EnglishPrompt,
    EnglishSkillId,
    Mark,
    Outcome,
)

ENGLISH_RULES = "english-v1"
ENGLISH_AI_PROMPT = "english-ai-mastery-v1"
_SCORES = {"correct": 2, "partial": 1, "incorrect": 0}


@dataclass(frozen=True)
class EnglishSkill:
    id: EnglishSkillId
    unit: str
    title: str
    hint: str


ENGLISH_SKILLS = (
    EnglishSkill("english-vocabulary", "词汇与语法", "词汇与词形", "先看词义，再根据句意检查词形和拼写。"),
    EnglishSkill("english-grammar", "词汇与语法", "基础语法", "找出主语、时间线索和句子结构，再选择合适的词形。"),
    EnglishSkill("english-reading", "阅读", "阅读理解", "先读问题，再回到原文寻找能够支持答案的信息。"),
    EnglishSkill("english-writing", "表达", "写作", "围绕题目组织内容，用完整句子表达，再检查拼写和语法。"),
    EnglishSkill("english-listening-text", "听说文本预习", "听力文本预习", "先理解对话场景和关键信息；本阶段没有音频。"),
    EnglishSkill("english-speaking-text", "听说文本预习", "口语表达草稿", "用短句回应场景，再试着补充原因；文本不能评估发音。"),
)
ENGLISH_SKILL_BY_ID = {skill.id: skill for skill in ENGLISH_SKILLS}


def normalized_answer(answer: str) -> str:
    # Preserve case and punctuation: these can be the knowledge point being assessed.
    return " ".join(unicodedata.normalize("NFKC", answer).split())


def english_key(prompt: EnglishPrompt) -> str:
    fields = {name: getattr(prompt, name) for name in EnglishPrompt.model_fields}
    data = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(data.encode()).hexdigest()


def english_correct(problem: EnglishProblem, answer: str) -> bool:
    if problem.mode == "open":
        raise ValueError("Subjective feedback cannot be scored")
    value = normalized_answer(answer)
    if problem.mode == "choice":
        value = value.upper()
    return value in accepted_answers(problem)


def accepted_answers(problem: EnglishProblem) -> set[str]:
    return {normalized_answer(answer) for answer in problem.answers}


def english_prompt(problem: EnglishProblem) -> EnglishPrompt:
    return EnglishPrompt.model_validate(problem.model_dump(include=set(EnglishPrompt.model_fields)))


def english_outcome(problem: EnglishProblem | None, answer: str | None, mark: str) -> str:
    if mark == "unclear" or problem is None:
        return "undetermined"
    if mark == "blank":
        return "blank"
    if problem.mode == "open":
        return "feedback"
    if not answer or not answer.strip():
        return "undetermined"
    return "correct" if english_correct(problem, answer) else "incorrect"


@dataclass(frozen=True)
class EnglishGrade:
    outcome: Outcome
    reason: str | None = None
    explanation: str | None = None
    reference: EnglishProblem | None = None
    mastery_skill: EnglishSkillId | None = None

    @property
    def score(self) -> int | None:
        return _SCORES.get(self.outcome) if self.mastery_skill else None


def grade_english(
    prompt: EnglishPrompt, answer: str | None, mark: Mark, assessment: EnglishAssessment | None,
    *, index: int, published: EnglishProblem | None = None,
) -> EnglishGrade:
    if mark == "unclear":
        return EnglishGrade("undetermined", "answer_unclear")
    if assessment is None:
        return EnglishGrade("undetermined", "missing_review")
    first, second, review = assessment.first, assessment.second, assessment.review
    if first is None or second is None:
        return EnglishGrade("undetermined", "missing_reference")
    references = (first, second) if published is None else (first, second, published)
    if any(english_prompt(reference) != prompt for reference in references):
        return EnglishGrade("undetermined", "source_mismatch")
    if any(accepted_answers(first) != accepted_answers(reference) for reference in references[1:]):
        return EnglishGrade("undetermined", "model_disagreement")
    if review is None or review.index != index:
        return EnglishGrade("undetermined", "missing_review")
    if not review.source_consistent:
        return EnglishGrade("undetermined", "source_mismatch")
    if not review.references_agree:
        return EnglishGrade("undetermined", "model_disagreement")
    if mark == "blank" and not answer and review.outcome == "blank":
        return EnglishGrade("blank")
    if mark != "present" or not answer or not answer.strip() or review.outcome == "blank":
        return EnglishGrade("undetermined", "answer_unclear")
    if review.outcome == "undetermined" or not review.explanation.strip():
        return EnglishGrade("undetermined", "review_inconclusive")
    if prompt.mode == "choice" and normalized_answer(answer).upper() not in list("ABCD"[:len(prompt.choices)]):
        return EnglishGrade("undetermined", "answer_unclear")
    matched = english_correct(first, answer)
    if (prompt.mode == "choice" and (review.outcome == "partial" or matched != (review.outcome == "correct"))) or (
        matched and review.outcome != "correct"
    ):
        return EnglishGrade("undetermined", "review_disagreement")
    if review.outcome == "partial" and not (review.partial_basis or "").strip():
        return EnglishGrade("undetermined", "invalid_partial")
    explanation = review.explanation
    if review.outcome == "partial":
        explanation = f"{explanation} {review.partial_basis}"
    mastery = review.mastery_skill if review.mastery_skill == prompt.skill and (review.mastery_basis or "").strip() else None
    return EnglishGrade(review.outcome, explanation=explanation, reference=first, mastery_skill=mastery)
