from typing import Protocol

import httpx
from pydantic import ValidationError

from edu_agent.demo import DemoAgent
from edu_app.config import Settings
from edu_domain.contracts import (
    AgentRequest,
    AgentResult,
    AnalysisResult,
    AnalyzeEnglish,
    AnalyzePage,
    EnglishAnalysisResult,
    EnglishFeedbackResult,
    EnglishPracticeResult,
    GenerateEnglish,
    PracticeResult,
    ReviewEnglish,
)


class AgentFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


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
        headers = {}
        if self.credential:
            from azure.core.exceptions import AzureError

            try:
                token = self.credential.get_token("https://ai.azure.com/.default")
            except AzureError as exc:
                raise AgentFailure("agent_unavailable", retryable=True) from exc
            headers["Authorization"] = f"Bearer {token.token}"
        try:
            response = self.client.post(self.url, json=request.model_dump(mode="json"), headers=headers)
        except httpx.RequestError as exc:
            raise AgentFailure("agent_unavailable", retryable=True) from exc
        if response.status_code == 424:
            raise AgentFailure("agent_no_result")
        if not response.is_success:
            raise AgentFailure(
                "agent_unavailable",
                retryable=response.status_code in (408, 429) or response.status_code >= 500,
            )
        if isinstance(request, AnalyzeEnglish):
            schema = EnglishAnalysisResult
        elif isinstance(request, GenerateEnglish):
            schema = EnglishPracticeResult
        elif isinstance(request, ReviewEnglish):
            schema = EnglishFeedbackResult
        else:
            schema = AnalysisResult if isinstance(request, AnalyzePage) else PracticeResult
        try:
            return schema.model_validate_json(response.content)
        except ValidationError as exc:
            raise AgentFailure("agent_invalid_result", retryable=True) from exc

    def close(self):
        self.client.close()


def build_agent(settings: Settings, credential) -> Agent:
    return HttpAgent(settings, credential) if settings.agent_url else DemoAgent()
