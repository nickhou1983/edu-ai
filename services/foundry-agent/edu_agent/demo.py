from base64 import b64encode
from functools import cache
from io import BytesIO
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

from edu_domain.contracts import (
    AgentRequest,
    AgentResult,
    AgentVersion,
    AnalysisResult,
    AnalyzedItem,
    AnalyzePage,
    GeneratePractice,
    Mark,
    PracticeResult,
    ProblemSpec,
    ReadItem,
    Region,
    Task,
)
from edu_domain.images import normalize_page
from edu_domain.math import verify

DEMO_VERSION = AgentVersion(model="synthetic-demo", prompt="fixture-v1", agent="local-demo")


class DemoItem(NamedTuple):
    task: Task
    expression: str
    answer: str | None
    mark: Mark = "present"
    steps: tuple[str, ...] = ()


_ROWS = (
    DemoItem("evaluate", "-8+3", "-5"),
    DemoItem("evaluate", "5-(-3)", "2"),
    DemoItem("evaluate", "(-2)-7", "-9"),
    DemoItem("simplify", "2x+3x-x", "4x"),
    DemoItem("simplify", "3(x-2)+x", "4x-2"),
    DemoItem("solve", "2(x+3)=14", "x=5", steps=("2x+6=14",)),
    DemoItem("solve", "3x-1=2", None, "blank"),
)
_LABELS = {"evaluate": "Calculate", "simplify": "Simplify", "solve": "Solve"}


@cache
def demo_page() -> bytes:
    image = Image.new("RGB", (1000, 1200), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1000, 164), fill="#e9eff9")
    draw.text((65, 45), "Review worksheet / 07", font=ImageFont.load_default(46), fill="#142c52")
    draw.text(
        (70, 113),
        "SYNTHETIC SAMPLE   |   GRADE 7 MATHEMATICS",
        font=ImageFont.load_default(21),
        fill="#345284",
    )
    for index, item in enumerate(_ROWS):
        top = 185 + index * 133
        draw.text(
            (65, top + 16),
            f"{index + 1:02}.  {_LABELS[item.task]}: {item.expression}",
            font=ImageFont.load_default(33),
            fill="#142c52",
        )
        response = "  ->  ".join((*item.steps, item.answer or ""))
        draw.text((115, top + 62), f"Work: {response}", font=ImageFont.load_default(29), fill="#2656ac")
        draw.line((55, top + 111, 945, top + 111), fill="#cbd8eb", width=2)
    draw.text(
        (65, 1144),
        "End of sample  /  No personal information",
        font=ImageFont.load_default(21),
        fill="#345284",
    )
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def demo_analysis() -> AnalysisResult:
    items: list[AnalyzedItem] = []
    for index, row in enumerate(_ROWS):
        top = 185 + index * 133
        region = Region(left=50, top=top * 1000 // 1200, right=950, bottom=(top + 111) * 1000 // 1200)
        spec = ProblemSpec(task=row.task, expression=row.expression, choices=[])
        source = ReadItem(
            label=str(index + 1),
            region=region,
            stem=f"{_LABELS[row.task]}: {row.expression}",
            task=row.task,
            expression=row.expression,
            choices=[],
            readable=True,
            mark=row.mark,
            answer=row.answer,
            steps=list(row.steps),
        )
        items.append(AnalyzedItem(source=source, spec=spec, candidate=verify(spec).expected))
    return AnalysisResult(
        version="1", operation="analyze_submission", quality="readable", items=items, provenance=DEMO_VERSION
    )


def demo_practice(request: GeneratePractice) -> PracticeResult:
    items: list[ProblemSpec] = []
    for position in range(1, request.count + 2):
        n = position + 4
        task: Task = "evaluate"
        if request.skill == "rational-sum":
            expression = f"-{n + 2}+2" if request.difficulty == "foundation" else f"-{n}+2-(-3)"
        elif request.skill == "rational-product":
            expression = f"-{n}*3" if request.difficulty == "foundation" else f"-{n}*6/3"
        elif request.skill == "rational-mixed":
            expression = f"-{n}^2" if request.difficulty == "foundation" else f"2+3*(-{n})"
        elif request.skill == "like-terms":
            task = "simplify"
            expression = f"{n}x-2x" if request.difficulty == "foundation" else f"{n}x+2-4x"
        elif request.skill == "distribution":
            task = "simplify"
            expression = f"{n}(x-3)" if request.difficulty == "foundation" else f"{n}(x-3)+2x"
        else:
            task = "solve"
            expression = f"{n}x+3=18" if request.difficulty == "foundation" else f"{n}(x+2)=3x+19"
        items.append(ProblemSpec(task=task, expression=expression, choices=[]))
    return PracticeResult(version="1", operation="generate_practice", provenance=DEMO_VERSION, items=items)


class DemoAgent:
    def invoke(self, request: AgentRequest) -> AgentResult:
        from edu_domain.contracts import DiscoverKnowledge, GenerateKnowledge, ReviewKnowledge

        if isinstance(request, (DiscoverKnowledge, GenerateKnowledge, ReviewKnowledge)):
            from edu_agent.knowledge_demo import knowledge_demo

            return knowledge_demo(request)
        if not isinstance(request, (AnalyzePage, GeneratePractice)):
            from edu_agent.english_demo import english_demo

            return english_demo(request)
        if isinstance(request, GeneratePractice):
            return demo_practice(request)
        if request.ai_only or request.review_items is not None or request.image_base64 != _encoded_sample():
            raise DemoImageOnly
        return demo_analysis()


@cache
def _encoded_sample() -> str:
    return b64encode(normalize_page(demo_page())).decode("ascii")


class DemoImageOnly(ValueError):
    pass
