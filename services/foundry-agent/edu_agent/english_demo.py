from base64 import b64encode
from functools import cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from edu_agent.demo import DEMO_VERSION, DemoImageOnly
from edu_domain.contracts import (
    AnalyzeEnglish,
    EnglishAnalysisResult,
    EnglishAnalyzedItem,
    EnglishFeedback,
    EnglishFeedbackResult,
    EnglishPracticeResult,
    EnglishProblem,
    EnglishReadItem,
    GenerateEnglish,
    Region,
    ReviewEnglish,
)
from edu_domain.images import normalize_page

DEMO_FEEDBACK = EnglishFeedback(
    strengths=["你用完整句子表达了主要信息。（本地合成演示）"],
    improvements=["检查句首大写，并试着用 because 补充一个原因。"],
    suggested_response="I like English because I can share my ideas with new friends.",
)


def demo_problems(request: GenerateEnglish) -> list[EnglishProblem]:
    problems = []
    for i in range(request.count):
        skill = request.skill
        mode = "fill"
        context = ""
        choices = []
        if skill == "english-vocabulary":
            word = ["apple", "book", "school", "friend", "family"][i]
            instruction = f"按原样正确拼写单词：{word}"
            answers = [word]
        elif skill == "english-grammar":
            subject, verb = [("I", "am"), ("She", "is"), ("They", "are"), ("He", "is"), ("We", "are")][i]
            instruction = f"选择正确的 be 动词：{subject} ___ a student." if i < 2 else f"填入 be 动词：{subject} ___ happy."
            if i < 2:
                mode, choices, answers = "choice", ["am", "is", "are"], ["A" if i == 0 else "B"]
            else:
                answers = [verb]
        elif skill == "english-reading":
            name, number = [("Sam", "two"), ("Amy", "three"), ("Ben", "four"), ("May", "five"), ("Leo", "six")][i]
            context = f"{name} has {number} books. The books are on the desk."
            instruction, answers = f"How many books does {name} have? Use one word.", [number]
        else:
            mode, answers = "open", []
            if skill == "english-writing":
                instruction = f"Write {3 + i} short sentences about your school day."
            elif skill == "english-listening-text":
                context = f"A: What time do you go to school? B: At {7 + i} o'clock."
                instruction = "阅读这段听力预习文本，用一句英语复述上学时间。当前没有音频。"
            else:
                instruction = f"口语草稿场景 {i + 1}：向新同学介绍你喜欢的一门课，写出你准备说的两句话。"
        problems.append(EnglishProblem(
            skill=skill, mode=mode, instruction=instruction, context=context, choices=choices,
            answers=answers, explanation="本地合成演示参考；实际 AI 反馈需连接 Foundry。",
        ))
    return problems


@cache
def english_demo_page() -> bytes:
    image = Image.new("RGB", (1100, 900), "white")
    draw = ImageDraw.Draw(image)
    lines = [
        "GRADE 7 ENGLISH / SYNTHETIC SAMPLE",
        "1. Spell this word: apple     Answer: apple",
        "2. Choose: She ___ a student. A.am B.is C.are",
        "   Answer: A",
        "3. Sam has two books. How many books? Answer: two",
        "4. Write about English. Answer: I like English.",
        "No personal information / Local demo only",
    ]
    for i, line in enumerate(lines):
        draw.text((40, 50 + i * 105), line, font=ImageFont.load_default(27), fill="#143c58")
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def english_demo(request: AnalyzeEnglish | GenerateEnglish | ReviewEnglish):
    if isinstance(request, GenerateEnglish):
        return EnglishPracticeResult(
            version="1", operation=request.operation, provenance=DEMO_VERSION, items=demo_problems(request),
        )
    if isinstance(request, ReviewEnglish):
        return EnglishFeedbackResult(
            version="1", operation=request.operation, provenance=DEMO_VERSION, feedback=DEMO_FEEDBACK,
        )
    if request.image_base64 != b64encode(normalize_page(english_demo_page())).decode("ascii"):
        raise DemoImageOnly
    items = []
    for i, skill in enumerate(("english-vocabulary", "english-grammar", "english-reading", "english-writing")):
        problem = demo_problems(GenerateEnglish(
            version="1", operation="generate_english", skill=skill, difficulty="foundation", count=3,
        ))[0 if i != 1 else 1]
        answer = ["apple", "A", "two", "I like English."][i]
        source = EnglishReadItem(
            label=str(i + 1), region=Region(left=20, top=150 + i * 160, right=980, bottom=300 + i * 160),
            prompt=problem.model_dump(include={"skill", "mode", "instruction", "context", "choices"}),
            readable=True, mark="present", answer=answer,
        )
        items.append(EnglishAnalyzedItem(source=source, problem=problem, feedback=DEMO_FEEDBACK if i == 3 else None))
    return EnglishAnalysisResult(
        version="1", operation=request.operation, quality="readable", provenance=DEMO_VERSION, items=items,
    )
