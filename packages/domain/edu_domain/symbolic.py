import ast
import functools
import json
import logging
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from threading import Lock

from edu_domain.contracts import Calculation, CalculationResult

logger = logging.getLogger(__name__)
_TRANSLATION = str.maketrans({"−": "-", "×": "*", "÷": "/", "·": "*"})
_TOOL_LOCK = Lock()


class CalculationError(ValueError):
    pass


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value.translate(_TRANSLATION)).strip()
    text = text.replace("^", "**")
    while "|" in text:
        replacement = re.sub(r"\|([^|]+)\|", r"Abs(\1)", text, count=1)
        if replacement == text:
            raise CalculationError("ambiguous_absolute_value")
        text = replacement
    text = re.sub(r"(?<=\d)(?=[A-Za-z(])|(?<=\))(?=[A-Za-z(0-9])", "*", text)
    return text


def _compute(request: Calculation) -> str:
    import sympy as sp

    functions = {
        "Abs": sp.Abs, "abs": sp.Abs, "sqrt": sp.sqrt, "binomial": sp.binomial,
        "factorial": sp.factorial, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan,
        "exp": sp.exp, "log": sp.log,
    }
    constants = {"pi": sp.pi, "E": sp.E, "I": sp.I}
    symbols = {name: sp.Symbol(name) for name in request.variables}
    if len(symbols) != len(request.variables) or symbols.keys() & (functions.keys() | constants.keys()):
        raise CalculationError("invalid_symbols")

    def parse(text, *, apply_bindings=True):
        try:
            tree = ast.parse(_normalize(text), mode="eval")
        except (SyntaxError, RecursionError) as exc:
            raise CalculationError("invalid_expression") from exc
        if sum(1 for _ in ast.walk(tree)) > 256:
            raise CalculationError("calculation_budget")

        def value(node):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                token = ast.get_source_segment(_normalize(text), node)
                if not token or len(token) > 100:
                    raise CalculationError("calculation_budget")
                return sp.Rational(token)
            if isinstance(node, ast.Name):
                if node.id in symbols:
                    symbol = symbols[node.id]
                    return bindings.get(symbol, symbol) if apply_bindings else symbol
                if node.id in constants:
                    return constants[node.id]
                raise CalculationError("undeclared_symbol")
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                inner = value(node.operand)
                return inner if isinstance(node.op, ast.UAdd) else -inner
            if isinstance(node, ast.BinOp):
                left, right = value(node.left), value(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    if right == 0:
                        raise CalculationError("division_by_zero")
                    return left / right
                if isinstance(node.op, ast.Pow):
                    if left == 0 and right == 0:
                        raise CalculationError("undefined_power")
                    coefficient = left.as_coeff_Mul()[0]
                    if coefficient.is_Rational and right.is_Rational and coefficient not in (-1, 0, 1):
                        bits = max(abs(int(coefficient.p)).bit_length(), int(coefficient.q).bit_length())
                        if bits * abs(right.p) > 32768 * right.q:
                            raise CalculationError("calculation_budget")
                    if left.is_Add and right.is_Integer and abs(right) >= 256:
                        raise CalculationError("calculation_budget")
                    return left ** right
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                function = functions.get(node.func.id)
                if function is not None and not node.keywords and 1 <= len(node.args) <= 2:
                    args = [value(arg) for arg in node.args]
                    if node.func.id == "factorial" and args[0].is_Integer and args[0] >= 0:
                        size = int(args[0])
                        if size * size.bit_length() > 32768:
                            raise CalculationError("calculation_budget")
                    if node.func.id == "binomial" and len(args) == 2 and all(arg.is_Integer for arg in args):
                        n, k = (int(arg) for arg in args)
                        total = n if n >= 0 else k - n - 1
                        if 0 <= k <= total and min(k, total - k) * total.bit_length() > 32768:
                            raise CalculationError("calculation_budget")
                    return function(*args)
            raise CalculationError("unsafe_expression")

        result = value(tree.body)
        if result.has(sp.zoo, sp.nan, sp.oo, -sp.oo):
            raise CalculationError("undefined_result")
        return result

    bindings = {}
    for binding in request.bindings:
        if binding.symbol not in symbols or binding.symbol in bindings:
            raise CalculationError("invalid_bindings")
        bound = parse(binding.value, apply_bindings=False)
        if bound.free_symbols:
            raise CalculationError("binding_must_be_constant")
        bindings[symbols[binding.symbol]] = bound

    if request.operation == "solve":
        equations = request.expression.split("=")
        if len(equations) != 2 or not symbols:
            raise CalculationError("expected_equation")
        left, right = (parse(part).subs(bindings, simultaneous=True) for part in equations)
        result = sp.solve(left - right, list(symbols.values()), dict=True)
        return json.dumps(
            [{str(name): str(answer) for name, answer in solution.items()} for solution in result],
            sort_keys=True,
        )
    expression = parse(request.expression).subs(bindings, simultaneous=True)
    if request.operation == "equivalent":
        if not request.comparison:
            raise CalculationError("expected_comparison")
        other = parse(request.comparison).subs(bindings, simultaneous=True)
        return "true" if sp.simplify(expression - other) == 0 else "false"
    result = sp.simplify(sp.expand(expression)) if request.operation == "simplify" else sp.simplify(expression)
    if result.has(sp.zoo, sp.nan, sp.oo, -sp.oo):
        raise CalculationError("undefined_result")
    return str(result)


@functools.lru_cache(maxsize=128)
def _execute(payload: str) -> CalculationResult:
    try:
        process = subprocess.run(
            [
                sys.executable, "-I", "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); from edu_domain.symbolic import main; main()",
                str(Path(__file__).resolve().parents[1]),
            ],
            input=payload, text=True, capture_output=True, timeout=5,
            env={"PATH": os.defpath},
        )
    except subprocess.TimeoutExpired:
        logger.warning("math_tool_failed code=timeout")
        return CalculationResult(status="timeout", value=None, reason="calculation_timeout")
    except OSError:
        logger.warning("math_tool_failed code=unavailable")
        return CalculationResult(status="unavailable", value=None, reason="calculation_unavailable")
    if process.returncode != 0:
        logger.warning("math_tool_failed code=process_limit")
        return CalculationResult(status="unavailable", value=None, reason="calculation_process_failed")
    try:
        result = CalculationResult.model_validate_json(process.stdout)
    except ValueError:
        logger.warning("math_tool_failed code=invalid_result")
        return CalculationResult(status="unavailable", value=None, reason="calculation_invalid_result")
    if result.status != "ok":
        logger.warning("math_tool_failed code=%s", result.reason)
    return result


def calculate(request: Calculation) -> CalculationResult:
    with _TOOL_LOCK:
        result = _execute(request.model_dump_json())
        if result.status in ("timeout", "unavailable"):
            _execute.cache_clear()
        return result


def main():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    try:
        request = Calculation.model_validate_json(sys.stdin.buffer.read(20000))
        answer = _compute(request)
        if len(answer) > 4000:
            raise CalculationError("calculation_budget")
        result = CalculationResult(status="ok", value=answer, reason=None)
    except CalculationError as exc:
        result = CalculationResult(status="invalid", value=None, reason=str(exc))
    except (ValueError, TypeError, RecursionError, OverflowError):
        result = CalculationResult(status="invalid", value=None, reason="invalid_or_unsafe_calculation")
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
