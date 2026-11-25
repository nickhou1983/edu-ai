from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1"
RULES_VERSION = "math-v1"
MAX_PAGES = 4
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 500_000
MAX_IMAGE_PIXELS = 24_000_000
MAX_SOURCE_IMAGE_PIXELS = 64_000_000
MAX_ITEMS_PER_PAGE = 24

SkillId = Literal[
    "rational-sum", "rational-product", "rational-mixed", "like-terms", "distribution", "linear-equations"
]
Difficulty = Literal["foundation", "standard"]
Purpose = Literal["diagnose", "strengthen"]
Task = Literal["evaluate", "simplify", "solve"]
Mark = Literal["present", "blank", "unclear"]
Outcome = Literal["correct", "partial", "incorrect", "blank", "undetermined"]
JobStatus = Literal["queued", "running", "done", "failed"]
Expression = Annotated[str, Field(min_length=1, max_length=96)]
AnswerText = Annotated[str, Field(max_length=96)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProblemSpec(Contract):
    task: Task
    expression: Expression
    choices: Annotated[list[Expression], Field(max_length=4)]


class Region(Contract):
    """Image coordinates on a 0–1000 scale, independent of display size."""

    left: Annotated[int, Field(ge=0, le=1000)]
    top: Annotated[int, Field(ge=0, le=1000)]
    right: Annotated[int, Field(ge=0, le=1000)]
    bottom: Annotated[int, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def check_area(self):
        if self.left >= self.right or self.top >= self.bottom:
            raise ValueError("Invalid image region")
        return self


class ReadItem(Contract):
    label: Annotated[str, Field(min_length=1, max_length=24)]
    region: Region
    stem: Annotated[str, Field(max_length=500)]
    task: Task | None
    expression: Expression | None
    choices: Annotated[list[Expression], Field(max_length=4)]
    readable: bool
    mark: Mark
    answer: AnswerText | None
    steps: Annotated[list[Expression], Field(max_length=8)]


class ReadPage(Contract):
    quality: Literal["readable", "unclear"]
    items: Annotated[list[ReadItem], Field(max_length=MAX_ITEMS_PER_PAGE)]


class Candidate(Contract):
    index: Annotated[int, Field(ge=0, lt=MAX_ITEMS_PER_PAGE)]
    spec: ProblemSpec | None
    answer: AnswerText | None


class SolvePage(Contract):
    solutions: Annotated[list[Candidate], Field(max_length=MAX_ITEMS_PER_PAGE)]


class AnalyzedItem(Contract):
    source: ReadItem
    spec: ProblemSpec | None
    candidate: AnswerText | None


class AnalyzePage(Contract):
    version: Literal["1"]
    operation: Literal["analyze_submission"]
    image_base64: Annotated[str, Field(min_length=4, max_length=((MAX_PAGE_BYTES + 2) // 3) * 4)]


class GeneratePractice(Contract):
    version: Literal["1"]
    operation: Literal["generate_practice"]
    skill: SkillId
    purpose: Purpose
    difficulty: Difficulty
    count: Annotated[int, Field(ge=3, le=5)]


class AgentVersion(Contract):
    model: Annotated[str, Field(min_length=1, max_length=128)]
    prompt: Annotated[str, Field(min_length=1, max_length=64)]
    agent: Annotated[str, Field(min_length=1, max_length=128)]


class AnalysisResult(Contract):
    version: Literal["1"]
    operation: Literal["analyze_submission"]
    quality: Literal["readable", "unclear"]
    items: Annotated[list[AnalyzedItem], Field(max_length=MAX_ITEMS_PER_PAGE)]
    provenance: AgentVersion


class PracticeCandidates(Contract):
    items: Annotated[list[ProblemSpec], Field(max_length=12)]


class PracticeResult(PracticeCandidates):
    version: Literal["1"]
    operation: Literal["generate_practice"]
    provenance: AgentVersion


EnglishSkillId = Literal[
    "english-vocabulary", "english-grammar", "english-reading",
    "english-writing", "english-listening-text", "english-speaking-text",
]
ENGLISH_OBJECTIVE_SKILLS = ("english-vocabulary", "english-grammar", "english-reading")
EnglishText = Annotated[str, Field(min_length=1, max_length=4000)]


class EnglishPrompt(Contract):
    skill: EnglishSkillId
    mode: Literal["choice", "fill", "open"]
    instruction: Annotated[str, Field(min_length=1, max_length=1000)]
    context: Annotated[str, Field(max_length=5000)]
    choices: Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(max_length=4)]

    @model_validator(mode="after")
    def check_format(self):
        objective = self.skill in ENGLISH_OBJECTIVE_SKILLS
        if objective == (self.mode == "open"):
            raise ValueError("Only vocabulary, grammar and reading support objective assessment")
        if self.mode == "choice":
            if len(self.choices) < 2 or len(set(self.choices)) != len(self.choices):
                raise ValueError("Choice questions need distinct options")
        elif self.choices:
            raise ValueError("Non-choice questions cannot contain options")
        if self.skill in ("english-reading", "english-listening-text") and not self.context.strip():
            raise ValueError("Reading and listening preparation require source text")
        return self


class EnglishProblem(EnglishPrompt):
    answers: Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(max_length=6)]
    explanation: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def check_answers(self):
        if self.mode == "open":
            if self.answers:
                raise ValueError("Subjective activities must not have a scored answer key")
        elif not self.answers or any(not answer.strip() for answer in self.answers):
            raise ValueError("Objective questions require accepted answers")
        if self.mode == "choice" and (
            len(self.answers) != 1 or self.answers[0] not in list("ABCD"[:len(self.choices)])
        ):
            raise ValueError("Choice key must identify exactly one option")
        return self


class EnglishFeedback(Contract):
    strengths: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(min_length=1, max_length=4)]
    improvements: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(min_length=1, max_length=4)]
    suggested_response: EnglishText


class EnglishReadItem(Contract):
    label: Annotated[str, Field(min_length=1, max_length=24)]
    region: Region
    prompt: EnglishPrompt | None
    readable: bool
    mark: Mark
    answer: EnglishText | None


class EnglishReadPage(Contract):
    quality: Literal["readable", "unclear"]
    items: Annotated[list[EnglishReadItem], Field(max_length=12)]


class EnglishSolution(Contract):
    index: Annotated[int, Field(ge=0, lt=12)]
    unambiguous: bool
    answers: Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(max_length=6)]
    explanation: Annotated[str, Field(min_length=1, max_length=1000)]


class EnglishSolutions(Contract):
    solutions: Annotated[list[EnglishSolution], Field(max_length=12)]


class EnglishReviewEntry(Contract):
    index: Annotated[int, Field(ge=0, lt=12)]
    feedback: EnglishFeedback


class EnglishReviews(Contract):
    reviews: Annotated[list[EnglishReviewEntry], Field(max_length=12)]


class EnglishAnalyzedItem(Contract):
    source: EnglishReadItem
    problem: EnglishProblem | None
    feedback: EnglishFeedback | None


class AnalyzeEnglish(Contract):
    version: Literal["1"]
    operation: Literal["analyze_english"]
    image_base64: Annotated[str, Field(min_length=4, max_length=((MAX_PAGE_BYTES + 2) // 3) * 4)]


class GenerateEnglish(Contract):
    version: Literal["1"]
    operation: Literal["generate_english"]
    skill: EnglishSkillId
    difficulty: Difficulty
    count: Annotated[int, Field(ge=3, le=5)]


class ReviewEnglish(Contract):
    version: Literal["1"]
    operation: Literal["review_english"]
    prompt: EnglishPrompt
    answer: EnglishText

    @model_validator(mode="after")
    def require_subjective(self):
        if self.prompt.mode != "open":
            raise ValueError("AI feedback is reserved for subjective activities")
        return self


class EnglishAnalysisResult(Contract):
    version: Literal["1"]
    operation: Literal["analyze_english"]
    quality: Literal["readable", "unclear"]
    items: Annotated[list[EnglishAnalyzedItem], Field(max_length=12)]
    provenance: AgentVersion


class EnglishCandidates(Contract):
    items: Annotated[list[EnglishProblem], Field(max_length=10)]


class EnglishPracticeResult(EnglishCandidates):
    version: Literal["1"]
    operation: Literal["generate_english"]
    provenance: AgentVersion


class EnglishFeedbackResult(Contract):
    version: Literal["1"]
    operation: Literal["review_english"]
    feedback: EnglishFeedback
    provenance: AgentVersion


Subject = Literal["math", "english"]
KnowledgeId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]
KnowledgeTitle = Annotated[str, Field(min_length=2, max_length=80)]


class KnowledgeDescriptor(Contract):
    id: KnowledgeId
    title: KnowledgeTitle
    unit: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=600)]

    @field_validator("title", "unit", "description", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class KnowledgeCandidate(Contract):
    existing_id: KnowledgeId | None
    title: KnowledgeTitle
    unit: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=600)]

    @field_validator("title", "unit", "description", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class KnowledgeDiscoveryItem(Contract):
    label: Annotated[str, Field(min_length=1, max_length=32)]
    region: Region
    stem: Annotated[str, Field(min_length=1, max_length=2000)]
    points: Annotated[list[KnowledgeCandidate], Field(min_length=1, max_length=4)]
    feedback: EnglishFeedback | None


class KnowledgeDiscoveries(Contract):
    quality: Literal["readable", "unclear"]
    items: Annotated[list[KnowledgeDiscoveryItem], Field(max_length=MAX_ITEMS_PER_PAGE)]


class DiscoverKnowledge(Contract):
    version: Literal["1"]
    operation: Literal["discover_knowledge"]
    subject: Subject
    image_base64: Annotated[str, Field(min_length=4, max_length=((MAX_PAGE_BYTES + 2) // 3) * 4)]
    catalog: Annotated[list[KnowledgeDescriptor], Field(max_length=250)]


class KnowledgeDiscoveryResult(KnowledgeDiscoveries):
    version: Literal["1"]
    operation: Literal["discover_knowledge"]
    provenance: AgentVersion


class ReferenceExercise(Contract):
    instruction: Annotated[str, Field(min_length=1, max_length=1000)]
    context: Annotated[str, Field(max_length=5000)]
    reference_answer: Annotated[str, Field(min_length=1, max_length=4000)]
    explanation: Annotated[str, Field(min_length=1, max_length=2000)]


class ReferenceExercises(Contract):
    items: Annotated[list[ReferenceExercise], Field(min_length=3, max_length=5)]


class GenerateKnowledge(Contract):
    version: Literal["1"]
    operation: Literal["generate_knowledge"]
    subject: Subject
    point: KnowledgeDescriptor
    difficulty: Difficulty
    count: Annotated[int, Field(ge=3, le=5)]


class KnowledgePracticeResult(ReferenceExercises):
    version: Literal["1"]
    operation: Literal["generate_knowledge"]
    provenance: AgentVersion


class ReviewKnowledge(Contract):
    version: Literal["1"]
    operation: Literal["review_knowledge"]
    subject: Subject
    point: KnowledgeDescriptor
    exercise: ReferenceExercise
    answer: EnglishText


class KnowledgeFeedbackResult(Contract):
    version: Literal["1"]
    operation: Literal["review_knowledge"]
    feedback: EnglishFeedback
    provenance: AgentVersion


AgentRequest = Annotated[
    AnalyzePage | GeneratePractice | AnalyzeEnglish | GenerateEnglish | ReviewEnglish
    | DiscoverKnowledge | GenerateKnowledge | ReviewKnowledge,
    Field(discriminator="operation"),
]
AgentResult = (
    AnalysisResult | PracticeResult | EnglishAnalysisResult | EnglishPracticeResult | EnglishFeedbackResult
    | KnowledgeDiscoveryResult | KnowledgePracticeResult | KnowledgeFeedbackResult
)
