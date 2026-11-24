from fractions import Fraction

import pytest

from edu_agent.demo import demo_analysis
from edu_domain.contracts import ProblemSpec
from edu_domain.grading import grade_item
from edu_domain.math import MathError, check_answer, matches_printed_target, verify


@pytest.mark.parametrize(
    ("task", "expression", "answer", "unsimplified"),
    [
        ("evaluate", "-2^2+1/3", "-11/3", "-4+1/3"),
        ("simplify", "3(x-2)+x", "4x-6", "3(x-2)+x"),
        ("simplify", "(1/2)x+(1/3)x", "x*5/6", "(1/2)x+(1/3)x"),
        ("solve", "2(x+3)＝14", "x=4", "x=8-4"),
    ],
)
def test_verify_requires_a_valid_final_form(task, expression, answer, unsimplified):
    verified = verify(ProblemSpec(task=task, expression=expression, choices=[]))
    assert check_answer(verified, answer)
    assert not check_answer(verified, unsimplified)


@pytest.mark.parametrize(
    ("task", "expression"),
    [
        ("evaluate", "4/(2-2)"),
        ("evaluate", "0^0+1"),
        ("evaluate", "2⁵+1"),
        ("evaluate", "10000000*10000000"),
        ("simplify", "(x-1)^0+x"),
        ("simplify", "x/(x-x+1)+x"),
        ("simplify", "x*x"),
        ("solve", "2(x+1)=2x+2"),
        ("solve", "x^2+x=2"),
    ],
)
def test_verify_rejects_unsupported_problems(task, expression):
    with pytest.raises(MathError):
        verify(ProblemSpec(task=task, expression=expression, choices=[]))


def test_choices_have_one_correct_answer_and_are_distinct_by_value():
    spec = ProblemSpec(task="evaluate", expression="1/4+1/4", choices=["3/4", "1/2", "-1/2"])
    problem = verify(spec)
    assert problem.value == {0: Fraction(1, 2)}
    assert check_answer(problem, "ｂ")
    with pytest.raises(MathError):
        verify(spec.model_copy(update={"choices": ["1/2", "0.5", "3/4"]}))
    with pytest.raises(MathError):
        verify(spec.model_copy(update={"choices": ["-1/2", "3/4"]}))


@pytest.mark.parametrize(
    ("task", "stem", "expression", "matches"),
    [
        ("evaluate", "计算：（−2）^2＝____。", "(-2)^2", True),
        ("simplify", "化简：x+x", "x+x", True),
        ("solve", "Solve: 2(x+3) = 14.", "2(x+3)=14", True),
        ("solve", "解方程：2x+3=7，求 x 的值。", "2x+3=7", True),
        ("solve", "Solve: 2x=3=", "2x=3", False),
        ("evaluate", "计算：3+2 和 1+4。", "3+2", False),
        ("evaluate", "计算：|2-3|", "2-3", False),
        ("evaluate", "求 3+5 的相反数。", "3+5", False),
        ("simplify", "化简：3x+2y", "3x+2", False),
    ],
)
def test_match_single_transcribed_target(task, stem, expression, matches):
    assert matches_printed_target(task, stem, expression) is matches


def test_grading_disagrees_with_candidate_or_transcription_without_counting_it_wrong():
    item = demo_analysis().items[0]
    assert item.spec is not None
    wrong_ai_answer = item.model_copy(update={"candidate": "-6"})
    wrong_ai_expression = item.model_copy(
        update={"spec": item.spec.model_copy(update={"expression": "-7+2"})}
    )
    misparsed_transcription = item.model_copy(
        update={"source": item.source.model_copy(update={"stem": "Calculate: 8-3"})}
    )
    for conflicting in (wrong_ai_answer, wrong_ai_expression, misparsed_transcription):
        grade = grade_item(conflicting)
        assert (grade.outcome, grade.score) == ("undetermined", None)


def test_grading_distinguishes_confirmed_blank_unclear_and_work_in_progress():
    _, _, _, _, _, partial, blank = demo_analysis().items
    assert (grade_item(partial).outcome, grade_item(partial).score) == ("partial", 1)
    assert (grade_item(blank).outcome, grade_item(blank).score) == ("blank", None)
    for item in (
        blank.model_copy(update={"source": blank.source.model_copy(update={"answer": "x=1"})}),
        blank.model_copy(update={"source": blank.source.model_copy(update={"mark": "unclear"})}),
    ):
        grade = grade_item(item)
        assert (grade.outcome, grade.score) == ("undetermined", None)
