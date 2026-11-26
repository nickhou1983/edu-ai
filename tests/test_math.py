from fractions import Fraction
from unittest.mock import Mock

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


@pytest.mark.parametrize(
    "steps,expected",
    [
        (["2x+6=14 -> x=5"], "partial"),
        (["2x+6=14 => x=5"], "partial"),
        (["2x+6=16 -> x=5"], "incorrect"),
        (["2(x+3)=14 -> x=5"], "incorrect"),
    ],
)
def test_partial_credit_verifies_equations_inside_transcribed_arrow_chains(steps, expected):
    item = demo_analysis().items[5]
    item = item.model_copy(update={"source": item.source.model_copy(update={"steps": steps})})
    assert grade_item(item).outcome == expected


@pytest.mark.parametrize(("operation", "expression", "variables", "expected"), [
    ("evaluate", "binomial(8,2)", [], "28"),
    ("evaluate", "(-1)^2026+(-2)^3/4-Abs(-3)*(-3^2)", [], "26"),
    ("simplify", "(6*m-5*n)-(7*m-8*n)", ["m", "n"], "-m + 3*n"),
    ("simplify", "5*(3*x^2*y-x*y^2)-4*(-x*y^2+2*x^2*y)", ["x", "y"], "x*y*(7*x - y)"),
    ("evaluate", "10*m/(m-3)", ["m"], "10*m/(m - 3)"),
    ("evaluate", "(-1)^1000001", [], "-1"),
])
def test_symbolic_tools_cover_screenshot_question_families(operation, expression, variables, expected):
    from edu_domain.contracts import Calculation
    from edu_domain.symbolic import calculate

    result = calculate(Calculation(
        operation=operation, expression=expression, variables=variables, bindings=[], comparison=None,
    ))
    assert result.status == "ok", result
    assert result.value == expected


@pytest.mark.parametrize("expression", [
    "__import__('os').system('whoami')", "x.__class__", "(lambda: 1)()",
    "[x for x in range(3)]", "open('/etc/passwd')", "2^1000000", "1/0", "0^0",
])
def test_symbolic_tools_reject_code_execution_and_unsafe_calculations(expression):
    from edu_domain.contracts import Calculation
    from edu_domain.symbolic import calculate

    result = calculate(Calculation(
        operation="evaluate", expression=expression, variables=["x"], bindings=[], comparison=None,
    ))
    assert result.status == "invalid"
    assert result.value is None
    assert result.reason


def test_open_assessment_grades_substitution_without_claiming_deterministic_mastery(open_math_item):
    grade = grade_item(open_math_item)
    assert (grade.outcome, grade.score, grade.reference) == ("incorrect", 0, "2")
    assert grade.verification == "ai_review" and grade.tool_checked
    assert grade.problem is None
    assert grade_item(open_math_item, correction="2").score is None


@pytest.mark.parametrize(("change", "reason"), [
    ({"source_consistent": False}, "source_mismatch"),
    ({"references_agree": False}, "model_disagreement"),
    ({"unambiguous": False}, "missing_information"),
    ({"outcome": "partial", "steps": []}, "invalid_partial"),
])
def test_open_assessment_keeps_ambiguity_and_disagreement_ungraded(open_math_item, change, reason):
    assessment = open_math_item.assessment
    item = open_math_item.model_copy(update={
        "assessment": assessment.model_copy(update={"review": assessment.review.model_copy(update=change)})
    })
    grade = grade_item(item)
    assert (grade.outcome, grade.score, grade.reason) == ("undetermined", None, reason)


def test_open_assessment_rechecks_reference_answers_and_tool_evidence(open_math_item):
    assessment = open_math_item.assessment
    for change in [
        {"second": assessment.second.model_copy(update={"answer": "-2"})},
        {"first_tool": assessment.first_tool.model_copy(update={"value": "99"})},
    ]:
        grade = grade_item(open_math_item.model_copy(update={"assessment": assessment.model_copy(update=change)}))
        assert (grade.outcome, grade.score) == ("undetermined", None)


@pytest.mark.parametrize(("answer", "outcome"), [("-2", "correct"), ("2", "incorrect")])
def test_ai_verdict_cannot_contradict_numeric_answer_comparison(open_math_item, answer, outcome):
    assessment = open_math_item.assessment
    item = open_math_item.model_copy(update={
        "source": open_math_item.source.model_copy(update={"answer": answer}),
        "assessment": assessment.model_copy(update={
            "review": assessment.review.model_copy(update={"outcome": outcome}),
        }),
    })
    grade = grade_item(item)
    assert (grade.outcome, grade.reason, grade.score) == ("undetermined", "review_disagreement", None)


def test_open_semantic_assessment_has_no_topic_or_expression_whitelist(open_math_item):
    assessment = open_math_item.assessment
    answer = "两组对边分别平行，因此四边形为平行四边形。"
    first = assessment.first.model_copy(update={"answer": answer, "calculation": None})
    review = assessment.review.model_copy(update={"outcome": "correct", "steps": [answer]})
    item = open_math_item.model_copy(update={
        "source": open_math_item.source.model_copy(update={
            "answer": answer,
            "question": open_math_item.source.question.model_copy(update={
                "text": "四边形ABCD中，AB平行于CD，AD平行于BC。证明它是平行四边形。",
                "goal": "证明四边形为平行四边形", "conditions": ["AB平行于CD", "AD平行于BC"],
                "parts": [], "points": [],
            }),
        }),
        "assessment": assessment.model_copy(update={
            "first": first, "second": first.model_copy(), "review": review,
            "first_tool": None, "second_tool": None,
        }),
    })
    grade = grade_item(item)
    assert (grade.outcome, grade.verification, grade.tool_checked) == ("correct", "ai_review", False)


def test_symbolic_tool_timeout_is_explicit(monkeypatch):
    import subprocess

    from edu_domain import symbolic
    from edu_domain.contracts import Calculation

    request = Calculation(operation="evaluate", expression="9876543+1", variables=[], bindings=[], comparison=None)
    monkeypatch.setattr(symbolic.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("math", 5)))
    result = symbolic.calculate(request)
    assert (result.status, result.value, result.reason) == ("timeout", None, "calculation_timeout")


def test_failed_calculation_prevents_ai_grading_even_when_review_agrees(open_math_item):
    from edu_domain.contracts import CalculationResult

    assessment = open_math_item.assessment
    calculation = assessment.first.calculation.model_copy(update={"expression": "1/0"})
    failed = CalculationResult(status="invalid", value=None, reason="division_by_zero")
    item = open_math_item.model_copy(update={"assessment": assessment.model_copy(update={
        "first": assessment.first.model_copy(update={"calculation": calculation}), "first_tool": failed,
    })})
    grade = grade_item(item)
    assert (grade.outcome, grade.score, grade.reason) == ("undetermined", None, "tool_failure")


def test_failed_answer_comparison_is_not_silently_treated_as_semantic_agreement(monkeypatch, open_math_item):
    from edu_domain import grading
    from edu_domain.contracts import CalculationResult

    original = grading.calculate

    def calculate(request):
        if request.operation == "equivalent":
            return CalculationResult(status="timeout", value=None, reason="calculation_timeout")
        return original(request)

    monkeypatch.setattr(grading, "calculate", calculate)
    grade = grading.grade_item(open_math_item)
    assert (grade.outcome, grade.score, grade.reason) == ("undetermined", None, "tool_failure")


def test_intermediate_simplification_is_not_mistaken_for_the_final_substituted_answer(open_math_item):
    from edu_domain.symbolic import calculate

    assessment = open_math_item.assessment
    calculation = assessment.first.calculation.model_copy(update={"operation": "simplify", "bindings": []})
    solution = assessment.first.model_copy(update={"calculation": calculation, "calculation_role": "intermediate"})
    item = open_math_item.model_copy(update={"assessment": assessment.model_copy(update={
        "first": solution, "second": solution.model_copy(),
        "first_tool": calculate(calculation), "second_tool": calculate(calculation),
    })})
    grade = grade_item(item)
    assert (grade.outcome, grade.reference, grade.verification) == ("incorrect", "2", "ai_review")
    assert grade.problem is None and grade.tool_checked


@pytest.mark.parametrize(("mark", "answer", "outcome", "reason"), [
    ("blank", None, "blank", None), ("unclear", None, "undetermined", "answer_unclear"),
])
def test_open_grading_distinguishes_blank_from_unreadable_work(open_math_item, mark, answer, outcome, reason):
    item = open_math_item.model_copy(update={"source": open_math_item.source.model_copy(update={
        "mark": mark, "answer": answer, "steps": [],
    }), "assessment": open_math_item.assessment.model_copy(update={
        "review": open_math_item.assessment.review.model_copy(update={"outcome": outcome}),
    })})
    grade = grade_item(item)
    assert (grade.outcome, grade.reason, grade.score) == (outcome, reason, None)


def test_tool_checks_substitution_before_cancelling_an_undefined_denominator():
    from edu_domain.contracts import Calculation, SymbolBinding
    from edu_domain.symbolic import calculate

    result = calculate(Calculation(
        operation="evaluate", expression="x/x", variables=["x"],
        bindings=[SymbolBinding(symbol="x", value="0")], comparison=None,
    ))
    assert (result.status, result.value, result.reason) == ("invalid", None, "division_by_zero")


@pytest.mark.parametrize("expression", [
    "2^1000000000000000000", "(2*x)^1000000000000000000",
    "(x+1)^1000000", "factorial(1000000000)",
])
def test_numeric_resource_budget_does_not_materialize_enormous_results(expression):
    from edu_domain.contracts import Calculation
    from edu_domain.symbolic import calculate

    result = calculate(Calculation(
        operation="evaluate", expression=expression, variables=["x"], bindings=[], comparison=None,
    ))
    assert (result.status, result.value, result.reason) == ("invalid", None, "calculation_budget")
