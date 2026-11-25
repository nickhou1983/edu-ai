import json
import unicodedata
from dataclasses import dataclass
from hashlib import sha256

from edu_domain.contracts import EnglishProblem, EnglishPrompt, EnglishSkillId

ENGLISH_RULES = "english-v1"


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
    return value in {normalized_answer(expected) for expected in problem.answers}


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
