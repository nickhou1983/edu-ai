import hashlib
import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

from edu_domain.contracts import Difficulty, ProblemSpec, SkillId, Task

_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]+)?|\.[0-9]+|[x=+\-*/^()]|\S", re.ASCII)
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z", re.ASCII)
_PRINTED_SPAN = re.compile(r"[0-9xX+\-*/^.=() \t]+")
_PRINTED_WORDING: dict[Task, tuple[str, str]] = {
    "evaluate": (
        r"(?:(?:请)?(?:计算(?:下列(?:各题|各式|算式|式子)(?:的值)?)?|求|填空)|calculate|compute|evaluate)?",
        r"(?:(?:的(?:值|结果|计算结果))?(?:等于(?:多少)?|为|是(?:多少)?)|的(?:值|结果|计算结果))?",
    ),
    "simplify": (
        r"(?:(?:请)?(?:化简(?:下列(?:各式|整式|式子))?|合并同类项|去括号(?:并化简)?)|simplify|expand and simplify)?",
        r"(?:的化简结果(?:为|是)?)?",
    ),
    "solve": (
        r"(?:(?:请)?(?:解(?:下列)?(?:一元一次)?方程|求解)|solve(?: the equation)?)?",
        r"(?:求(?:未知数)?\s*x\s*(?:的值)?)?",
    ),
}
_TRANSLATIONS = str.maketrans(
    {
        "−": "-",
        "×": "*",
        "÷": "/",
        "·": "*",
        **{digit: f"^{power}" for power, digit in enumerate("⁰¹²³⁴⁵⁶⁷⁸⁹")},
    }
)
_LETTERS = "ABCD"
_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2, "^": 4}
Poly = dict[int, Fraction]


class MathError(ValueError):
    pass


@dataclass(frozen=True)
class Number:
    value: Fraction


@dataclass(frozen=True)
class Variable:
    pass


@dataclass(frozen=True)
class Signed:
    sign: str
    value: "Expr"


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Expr"
    right: "Expr"


Expr = Number | Variable | Signed | Binary


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text.translate(_TRANSLATIONS))


def _tokens(text: str) -> list[str]:
    if len(text) > 96:
        raise MathError("Expression too long")
    raw = _TOKEN.findall(_normalize(text).lower().replace("**", "^"))
    if not raw or len(raw) > 64:
        raise MathError("Invalid number of tokens")
    tokens: list[str] = []
    for token in raw:
        left = tokens[-1] if tokens else ""
        if ((left == "x" or left == ")" or _NUMBER.fullmatch(left)) and token in ("x", "(")) or (
            left == ")" and _NUMBER.fullmatch(token)
        ):
            tokens.append("*")
        tokens.append(token)
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.index = 0

    def _peek(self) -> str:
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def _take(self) -> str:
        value = self._peek()
        self.index += 1
        return value

    def _parse_expr(self, precedence: int = 0) -> Expr:
        token = self._take()
        result: Expr
        if token in ("+", "-"):
            result = Signed(token, self._parse_expr(3))
        elif token == "(":
            result = self._parse_expr()
            if self._take() != ")":
                raise MathError("Missing closing parenthesis")
        elif token == "x":
            result = Variable()
        elif _NUMBER.fullmatch(token) and len(token.replace(".", "")) <= 8:
            result = Number(Fraction(token))
        else:
            raise MathError("Unsupported expression token")
        while (level := _PRECEDENCE.get(self._peek(), -1)) >= precedence:
            op = self._take()
            result = Binary(op, result, self._parse_expr(level if op == "^" else level + 1))
        return result

    def parse(self) -> Expr:
        expression = self._parse_expr()
        if self._peek():
            raise MathError("Unexpected expression token")
        return expression


def _parse(text: str) -> Expr:
    return _Parser(_tokens(text)).parse()


def _equation(text: str) -> tuple[Expr, Expr]:
    parts = _normalize(text).split("=")
    if len(parts) != 2:
        raise MathError("Expected one equals sign")
    return _parse(parts[0]), _parse(parts[1])


def _bounded(terms: Poly) -> Poly:
    if any(
        power > 4 or max(abs(value.numerator).bit_length(), value.denominator.bit_length()) > 96
        for power, value in terms.items()
    ):
        raise MathError("Result exceeds supported range")
    return {power: value for power, value in terms.items() if value}


def _sum(left: Poly, right: Poly, sign: int = 1) -> Poly:
    result = left.copy()
    for power, value in right.items():
        result[power] = result.get(power, Fraction(0)) + sign * value
    return _bounded(result)


def _product(left: Poly, right: Poly) -> Poly:
    result: Poly = {}
    for a, x in left.items():
        for b, y in right.items():
            result[a + b] = result.get(a + b, Fraction(0)) + x * y
    return _bounded(result)


def _evaluate(expr: Expr) -> Poly:
    if isinstance(expr, Number):
        return {0: expr.value} if expr.value else {}
    if isinstance(expr, Variable):
        return {1: Fraction(1)}
    if isinstance(expr, Signed):
        inner = _evaluate(expr.value)
        return inner if expr.sign == "+" else {power: -value for power, value in inner.items()}
    left = _evaluate(expr.left)
    if expr.op == "^":
        if not isinstance(expr.right, Number) or expr.right.value not in range(5):
            raise MathError("Supported powers are integers from 0 to 4")
        if not expr.right.value and (not left or _variables(expr.left)):
            raise MathError("A zero exponent requires a nonzero constant base")
        result = {0: Fraction(1)}
        for _ in range(int(expr.right.value)):
            result = _product(result, left)
        return result
    right = _evaluate(expr.right)
    if expr.op == "+":
        return _sum(left, right)
    if expr.op == "-":
        return _sum(left, right, -1)
    if expr.op == "*":
        return _product(left, right)
    if _variables(expr.right) or not right.get(0):
        raise MathError("Division requires a nonzero constant denominator")
    return _bounded({power: value / right[0] for power, value in left.items()})


def _variables(expr: Expr) -> bool:
    if isinstance(expr, Variable):
        return True
    if isinstance(expr, Signed):
        return _variables(expr.value)
    return isinstance(expr, Binary) and (_variables(expr.left) or _variables(expr.right))


def _ops(expr: Expr) -> list[Binary]:
    if isinstance(expr, Binary):
        return [expr, *_ops(expr.left), *_ops(expr.right)]
    return _ops(expr.value) if isinstance(expr, Signed) else []


def _linear(left: Expr, right: Expr) -> tuple[Fraction, Fraction]:
    left_value, right_value = _evaluate(left), _evaluate(right)
    if any(degree > 1 for part in (left_value, right_value) for degree in part):
        raise MathError("Only linear equations are supported")
    delta = _sum(left_value, right_value, -1)
    if not delta.get(1):
        raise MathError("A unique solution is required")
    return delta[1], delta.get(0, Fraction(0))


def _form(expr: Expr) -> str:
    if isinstance(expr, Number):
        return f"n:{expr.value}"
    if isinstance(expr, Variable):
        return "x"
    if isinstance(expr, Signed):
        return f"({expr.sign}{_form(expr.value)})"
    return f"({_form(expr.left)}{expr.op}{_form(expr.right)})"


def expression_signature(task: Task, text: str) -> str:
    if len(text) > 96:
        raise MathError("Expression too long")
    if task == "solve":
        left, right = _equation(text)
        return f"{_form(left)}={_form(right)}"
    return _form(_parse(text))


def matches_printed_target(task: Task, stem: str, expression: str) -> bool:
    text = _normalize(stem).strip().rstrip(" 。.!?;")
    text = re.sub(r"(?:_+|\(\s*\))$", "", text).rstrip()
    prefix, suffix = _PRINTED_WORDING[task]
    for span in _PRINTED_SPAN.finditer(text):
        value = span.group().strip()
        before = text[: span.start()].strip(" :,、\n\t").casefold()
        after = text[span.end() :].strip(" :,、\n\t").casefold()
        if not value or not re.fullmatch(prefix, before) or not re.fullmatch(suffix, after):
            continue
        if task != "solve":
            value = value.removesuffix("=").rstrip()
        try:
            return expression_signature(task, value) == expression_signature(task, expression)
        except MathError:
            return False
    return False


def _number_form(expr: Expr) -> bool:
    if isinstance(expr, Number):
        return True
    if isinstance(expr, Signed):
        return isinstance(expr.value, Number) or (
            isinstance(expr.value, Binary) and expr.value.op == "/" and _number_form(expr.value)
        )
    if isinstance(expr, Binary) and expr.op == "/":
        return all(
            isinstance(part, Number) or (isinstance(part, Signed) and isinstance(part.value, Number))
            for part in (expr.left, expr.right)
        )
    return False


def _monomial(expr: Expr) -> bool:
    if isinstance(expr, (Number, Variable)) or _number_form(expr):
        return True
    if isinstance(expr, Signed):
        return _monomial(expr.value)
    if expr.op == "^":
        return isinstance(expr.left, Variable) and isinstance(expr.right, Number) and expr.right.value >= 2
    if expr.op == "*":
        return (_number_form(expr.left) and _pure_variable_term(expr.right)) or (
            _number_form(expr.right) and _pure_variable_term(expr.left)
        )
    return expr.op == "/" and _monomial(expr.left) and _number_form(expr.right) and _variables(expr.left)


def _pure_variable_term(expr: Expr) -> bool:
    return isinstance(expr, Variable) or (isinstance(expr, Binary) and expr.op == "^" and _monomial(expr))


def _terms(expr: Expr) -> list[Expr]:
    if isinstance(expr, Binary) and expr.op == "+":
        return [*_terms(expr.left), *_terms(expr.right)]
    if isinstance(expr, Binary) and expr.op == "-":
        return [*_terms(expr.left), Signed("-", expr.right)]
    return [expr]


def _simplified(expr: Expr) -> bool:
    degrees: set[int] = set()
    terms = _terms(expr)
    for term in terms:
        if not _monomial(term):
            return False
        value = _evaluate(term)
        if not value:
            return len(terms) == 1 and _number_form(term)
        degree = next(iter(value))
        if degree in degrees:
            return False
        degrees.add(degree)
    return True


def _answer_value(task: Task, answer: str) -> tuple[Poly, bool]:
    answer = _normalize(answer).strip()
    if task == "solve" and "=" in answer:
        left, right = _equation(answer)
        if not isinstance(left, Variable):
            raise MathError("Expected x followed by the solution")
        expr = right
    else:
        expr = _parse(answer)
    if task != "simplify" and _variables(expr):
        raise MathError("Expected a numeric value")
    value = _evaluate(expr)
    return value, _simplified(expr) if task == "simplify" else _number_form(expr)


def _polynomial_text(value: Poly) -> str:
    terms: list[str] = []
    for power, coefficient in sorted(value.items(), reverse=True):
        size = abs(coefficient)
        sign = "-" if coefficient < 0 else "+" if terms else ""
        if power:
            literal = "x" if power == 1 else f"x^{power}"
            factor = "" if size == 1 else f"({size})" if size.denominator != 1 else str(size)
            terms.append(f"{sign}{factor}{literal}")
        else:
            terms.append(f"{sign}{size}")
    return "".join(terms) or "0"


@dataclass(frozen=True)
class Verified:
    spec: ProblemSpec
    skill: SkillId
    difficulty: Difficulty
    key: str
    expected: str
    value: Poly
    explanation: str


def verify(spec: ProblemSpec) -> Verified:
    task, text = spec.task, spec.expression
    signature = expression_signature(task, text)
    skill: SkillId
    if task == "solve":
        left, right = _equation(text)
        coefficient, constant = _linear(left, right)
        solution = -constant / coefficient
        value = _bounded({0: solution})
        count = len(_ops(left)) + len(_ops(right))
        skill = "linear-equations"
        expected = f"x={solution}"
        simplified = _polynomial_text({1: coefficient, 0: constant} if constant else {1: coefficient})
        explanation = f"等式整理为 {simplified}=0，所以 {expected}。"
    else:
        expr = _parse(text)
        operations = _ops(expr)
        count = len(operations)
        value = _evaluate(expr)
        expected = _polynomial_text(value)
        if task == "evaluate":
            if _variables(expr) or not operations:
                raise MathError("Expected a rational arithmetic problem")
            ops = {node.op for node in operations}
            if ops <= {"+", "-"}:
                skill = "rational-sum"
            elif ops <= {"*", "/"}:
                skill = "rational-product"
            else:
                skill = "rational-mixed"
            explanation = f"按运算顺序计算，结果是 {expected}。"
        else:
            if not _variables(expr) or not operations or _simplified(expr):
                raise MathError("Expected a polynomial that needs simplifying")
            distribute = any(
                node.op in ("*", "/", "^")
                and any(
                    isinstance(side, Binary) and side.op in ("+", "-") for side in (node.left, node.right)
                )
                for node in operations
            )
            skill = "distribution" if distribute else "like-terms"
            if distribute:
                count = sum(node.op in ("+", "-") for node in operations)
            else:
                terms = _terms(expr)
                degrees = [degree for term in terms if _monomial(term) for degree in _evaluate(term)]
                if len(set(degrees)) == len(degrees):
                    raise MathError("Expected like terms or distribution")
                count = len(terms) - 1
            explanation = (
                f"展开括号、合并同类项，得到 {expected}。" if distribute else f"合并同类项，得到 {expected}。"
            )

    if _answer_value(task, expected) != (value, True):
        raise MathError("The expected answer cannot be entered in a supported form")

    if spec.choices:
        if not 2 <= len(spec.choices) <= 4:
            raise MathError("Expected two to four choices")
        choices = [_answer_value(task, item) for item in spec.choices]
        distinct = {tuple(sorted(candidate.items())) for candidate, _ in choices}
        correct = [index for index, (candidate, form) in enumerate(choices) if candidate == value and form]
        if len(distinct) != len(choices) or len(correct) != 1:
            raise MathError("Choices must be distinct and have exactly one correct answer")
        expected = _LETTERS[correct[0]]
        explanation = f"{explanation}选 {expected}。"
    complexity = 3 if task == "solve" else 2
    difficulty: Difficulty = "standard" if count >= complexity else "foundation"
    key = hashlib.sha256(f"{task}:{signature}".encode()).hexdigest()
    return Verified(spec, skill, difficulty, key, expected, value, explanation)


def check_answer(problem: Verified, answer: str) -> bool:
    if problem.spec.choices:
        choice = unicodedata.normalize("NFKC", answer).strip().upper()
        if len(choice) != 1 or choice not in _LETTERS[: len(problem.spec.choices)]:
            raise MathError("Expected a single choice letter")
        return choice == problem.expected
    value, form = _answer_value(problem.spec.task, answer)
    return value == problem.value and form


def has_verified_step(problem: Verified, steps: list[str]) -> bool:
    if problem.spec.task != "solve":
        return False
    original = expression_signature("solve", problem.spec.expression)
    equations = [part for step in steps for part in re.split(r"\s*(?:->|=>)\s*", step)]
    for step in equations:
        try:
            signature = expression_signature("solve", step)
            if signature == original:
                continue
            left, right = _equation(step)
            coefficient, constant = _linear(left, right)
            return -constant / coefficient == problem.value.get(0, Fraction(0))
        except MathError:
            return False
    return False


def problem_text(spec: ProblemSpec) -> str:
    label = {"evaluate": "计算", "simplify": "化简", "solve": "解方程"}[spec.task]
    return f"{label}：{spec.expression}"
