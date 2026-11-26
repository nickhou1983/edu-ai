import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from edu_app.api import build_app
from edu_app.config import Settings
from edu_app.migration import migrate
from edu_app.runtime import Runtime

_POSTGRES_URL = os.environ.get("EDU_TEST_DATABASE_URL")
_BACKENDS = ("sqlite", "postgresql") if _POSTGRES_URL else ("sqlite",)


@contextmanager
def _database_url(backend):
    if backend == "sqlite":
        yield None
        return
    url = make_url(_POSTGRES_URL)
    schema = f"edu_test_{uuid4().hex}"
    admin = create_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        try:
            test_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
            yield test_url.render_as_string(hide_password=False)
        finally:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
    finally:
        admin.dispose()


@pytest.fixture(params=_BACKENDS)
def runtime(tmp_path, request):
    with _database_url(request.param) as database_url:
        settings = Settings(
            _env_file=None,
            mode="local",
            data_dir=tmp_path,
            database_url=database_url,
            agent_url=None,
        )
        app_runtime = Runtime(settings)
        try:
            migrate(app_runtime.db)
            yield app_runtime
        finally:
            app_runtime.close()


@pytest.fixture
def client(runtime):
    with TestClient(build_app(runtime), headers={"X-Demo-Student": "demo-a"}) as test_client:
        yield test_client


@pytest.fixture
def open_math_item():
    from edu_domain.contracts import (
        AnalyzedItem,
        Calculation,
        KnowledgeCandidate,
        MathQuestion,
        OpenAssessment,
        OpenReview,
        OpenSolution,
        ReadItem,
        Region,
        SymbolBinding,
    )
    from edu_domain.symbolic import calculate

    computation = Calculation(
        operation="evaluate", expression="-a^2*b+(-8*a*b^2-a^2*b)-2*(5*a*b^2-a^2*b)",
        variables=["a", "b"], bindings=[
            SymbolBinding(symbol="a", value="-1"), SymbolBinding(symbol="b", value="1/3"),
        ], comparison=None,
    )
    question = MathQuestion(
        text="先化简再求值：-a^2*b+(-8*a*b^2-a^2*b)-2*(5*a*b^2-a^2*b)，其中a=-1，b=1/3。",
        goal="化简并代入求值", conditions=["a=-1", "b=1/3"], parts=["化简", "求值"], diagram="",
        points=[KnowledgeCandidate(
            existing_id=None, title="整式化简与代入求值", unit="整式运算", description="展开括号合并同类项后代入求值。",
        )],
    )
    solution = OpenSolution(
        index=0, answer="2", explanation="化简得到-18*a*b^2，代入得到2。",
        assumptions=[], calculation=computation,
    )
    return AnalyzedItem(
        source=ReadItem(
            label="15", region=Region(left=0, top=0, right=1000, bottom=1000),
            stem=question.text, task=None, expression=None, choices=[], readable=True,
            mark="present", answer="-2", steps=["18*a*b^2"], question=question,
        ),
        spec=None, candidate=None,
        assessment=OpenAssessment(
            first=solution, second=solution.model_copy(),
            review=OpenReview(
                index=0, source_consistent=True, references_agree=True, unambiguous=True,
                outcome="incorrect", feedback="去括号时丢失了负号，正确化简结果为-18*a*b^2，代入应为2。",
                steps=["检查-2乘括号中的每一项。"],
            ),
            first_tool=calculate(computation), second_tool=calculate(computation),
        ),
    )
