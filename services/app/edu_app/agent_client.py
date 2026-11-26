import logging
from contextvars import ContextVar
from time import monotonic
from typing import NoReturn, Protocol

import httpx
from pydantic import ValidationError

from edu_agent.demo import DemoAgent
from edu_app.config import Settings
from edu_domain.agent_protocol import (
    JOB_ID_HEADER,
    MODEL_TIMEOUT_CODES,
    PAGE_READ_BUDGET_HEADER,
    retry_after_seconds,
)
from edu_domain.contracts import (
    AgentRequest,
    AgentResult,
    AnalysisResult,
    AnalyzeEnglish,
    AnalyzePage,
    DiscoverKnowledge,
    EnglishAnalysisResult,
    EnglishFeedbackResult,
    EnglishGradeResult,
    EnglishPracticeResult,
    GenerateEnglish,
    GenerateKnowledge,
    GradeEnglish,
    KnowledgeDiscoveryResult,
    KnowledgeFeedbackResult,
    KnowledgePracticeResult,
    PracticeResult,
    ReviewEnglish,
    ReviewKnowledge,
)

logger = logging.getLogger(__name__)
agent_job_id: ContextVar[str | None] = ContextVar("agent_job_id", default=None)


class AgentFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class Agent(Protocol):
    def invoke(self, request: AgentRequest) -> AgentResult: ...


class HttpAgent:
    def __init__(self, settings: Settings, credential):
        if not settings.agent_url:
            raise ValueError("Agent URL is required")
        self.url = httpx.URL(settings.agent_url)
        self.credential = credential
        if credential is not None and "api-version" not in self.url.params:
            self.url = self.url.copy_merge_params({"api-version": "v1"})
        self.client = httpx.Client(
            timeout=httpx.Timeout(settings.agent_timeout_seconds, connect=10), trust_env=False
        )

    def invoke(self, request: AgentRequest) -> AgentResult:
        started = monotonic()
        headers = {}
        job_id = agent_job_id.get()
        if job_id is not None:
            headers[JOB_ID_HEADER] = job_id
        fields = (job_id or "-", request.operation)
        if self.credential:
            from azure.core.exceptions import AzureError

            try:
                token = self.credential.get_token("https://ai.azure.com/.default")
            except AzureError as exc:
                logger.warning("agent_auth_failed job_id=%s operation=%s error_type=%s elapsed_ms=%d",
                               *fields, type(exc).__name__, (monotonic() - started) * 1000)
                raise AgentFailure("agent_unavailable", retryable=True) from exc
            headers["Authorization"] = f"Bearer {token.token}"
        try:
            timeout = self.client.timeout
            if request.version == "2":
                if timeout.read is not None:
                    headers[PAGE_READ_BUDGET_HEADER] = str(int(timeout.read))
            else:
                timeout = httpx.Timeout(min(timeout.read or 125, 125), connect=10)
            response = self.client.post(
                self.url, json=request.model_dump(mode="json"), headers=headers, timeout=timeout,
            )
        except httpx.RequestError as exc:
            code = "agent_timeout" if isinstance(exc, httpx.TimeoutException) else "agent_unavailable"
            logger.warning("agent_transport_failed job_id=%s operation=%s code=%s error_type=%s elapsed_ms=%d",
                           *fields, code, type(exc).__name__, (monotonic() - started) * 1000)
            raise AgentFailure(code, retryable=True) from exc
        if not response.is_success:
            self._raise_http_failure(
                response, fields, (monotonic() - started) * 1000,
                review_only=isinstance(request, (AnalyzePage, AnalyzeEnglish)) and request.review_items is not None,
            )
        if isinstance(request, DiscoverKnowledge):
            schema = KnowledgeDiscoveryResult
        elif isinstance(request, GenerateKnowledge):
            schema = KnowledgePracticeResult
        elif isinstance(request, ReviewKnowledge):
            schema = KnowledgeFeedbackResult
        elif isinstance(request, AnalyzeEnglish):
            schema = EnglishAnalysisResult
        elif isinstance(request, GenerateEnglish):
            schema = EnglishPracticeResult
        elif isinstance(request, ReviewEnglish):
            schema = EnglishFeedbackResult
        elif isinstance(request, GradeEnglish):
            schema = EnglishGradeResult
        else:
            schema = AnalysisResult if isinstance(request, AnalyzePage) else PracticeResult
        try:
            return schema.model_validate_json(response.content)
        except ValidationError as exc:
            raise AgentFailure("agent_invalid_result", retryable=True) from exc

    @staticmethod
    def _raise_http_failure(
        response: httpx.Response, fields: tuple[str, str], elapsed_ms: float, *, review_only: bool,
    ) -> NoReturn:
        status = response.status_code
        model_code = None
        if status == 503:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                if body["error"] in MODEL_TIMEOUT_CODES or body["error"] == "model_rate_limited":
                    model_code = body["error"]
        if status in (408, 504) or model_code in MODEL_TIMEOUT_CODES:
            code = "agent_timeout"
        elif status == 429 or model_code == "model_rate_limited":
            code = "agent_rate_limited"
        elif status == 424:
            code = "agent_no_result"
        elif review_only and status in (400, 422):
            code = "review_unavailable"
        else:
            code = "agent_unavailable"
        logger.warning(
            "agent_http_failed job_id=%s operation=%s code=%s http_status=%s model_code=%s elapsed_ms=%d",
            *fields, code, status, model_code or "-", elapsed_ms,
        )
        retry_after = None
        if code == "agent_rate_limited":
            retry_after = retry_after_seconds(response.headers.get("Retry-After"))
            if retry_after is None:
                retry_after = 60
        raise AgentFailure(
            code, retryable=status in (408, 429) or status >= 500, retry_after=retry_after,
        )

    def close(self):
        self.client.close()


def build_agent(settings: Settings, credential) -> Agent:
    return HttpAgent(settings, credential) if settings.agent_url else DemoAgent()
