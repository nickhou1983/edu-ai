import asyncio
import logging
import os
from contextlib import asynccontextmanager
from time import monotonic
from uuid import UUID

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from edu_agent.service import (
    DEFAULT_PAGE_TIMEOUT_SECONDS,
    AgentInputError,
    MathAgent,
    ModelError,
    model_schema,
)
from edu_domain.agent_protocol import (
    JOB_ID_HEADER,
    MAX_AGENT_METADATA_BYTES,
    PAGE_READ_BUDGET_HEADER,
    retry_after_seconds,
)
from edu_domain.contracts import MAX_PAGE_BYTES, AgentRequest, AnalysisResult, AnalyzePage

logger = logging.getLogger(__name__)
_REQUEST = TypeAdapter(AgentRequest)
_MAX_BODY = ((MAX_PAGE_BYTES + 2) // 3) * 4 + MAX_AGENT_METADATA_BYTES


class MathTimeouts(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDU_MATH_")
    model_timeout_seconds: int = Field(default=90, ge=1, le=300)
    page_timeout_seconds: int = Field(default=DEFAULT_PAGE_TIMEOUT_SECONDS, ge=1, le=850)

    def page_budget(self, worker_budget: str | None) -> int:
        if worker_budget and len(worker_budget) <= 3 and worker_budget.isascii() and worker_budget.isdecimal():
            return min(self.page_timeout_seconds, max(1, int(worker_budget) - 5))
        return self.page_timeout_seconds


class FoundryModel:
    def __init__(self, client: AsyncOpenAI, deployment: str, *, job_id: str = "-", operation: str = "-"):
        self.client = client
        self.deployment = deployment
        self.job_id = job_id
        self.operation = operation

    async def infer[T: BaseModel](
        self, schema: type[T], instructions: str, text: str, *, image: str | None = None
    ) -> tuple[T, str]:
        started = monotonic()
        fields = (self.job_id, self.operation, schema.__name__)
        try:
            result = await self._request(schema, instructions, text, image=image)
        except ModelError as exc:
            cause = exc.__cause__
            logger.warning(
                "model_call_failed code=%s retryable=%s job_id=%s operation=%s stage=%s elapsed_ms=%d "
                "error_type=%s http_status=%s",
                exc.code, exc.retryable, *fields, (monotonic() - started) * 1000,
                type(cause).__name__ if cause else "-", cause.status_code if isinstance(cause, APIStatusError) else "-",
            )
            raise
        except asyncio.CancelledError:
            logger.info("model_call_cancelled job_id=%s operation=%s stage=%s elapsed_ms=%d",
                        *fields, (monotonic() - started) * 1000)
            raise
        logger.info("model_call_completed job_id=%s operation=%s stage=%s elapsed_ms=%d",
                    *fields, (monotonic() - started) * 1000)
        return result

    async def _request[T: BaseModel](
        self, schema: type[T], instructions: str, text: str, *, image: str | None,
    ) -> tuple[T, str]:
        content = [{"type": "input_text", "text": text}]
        if image:
            content.append({"type": "input_image", "image_url": image, "detail": "high"})
        try:
            response = await self.client.responses.create(
                model=self.deployment,
                instructions=instructions,
                input=[{"role": "user", "content": content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema.__name__,
                        "schema": model_schema(schema),
                        "strict": True,
                    }
                },
                max_output_tokens=12000,
                store=False,
            )
        except APITimeoutError as exc:
            raise ModelError("model_sdk_timeout", retryable=True) from exc
        except (APIConnectionError, ClientAuthenticationError) as exc:
            raise ModelError("model_unreachable", retryable=True) from exc
        except APIStatusError as exc:
            if exc.status_code in (408, 504):
                raise ModelError("model_http_timeout", retryable=True) from exc
            if exc.status_code == 429:
                delay = retry_after_seconds(exc.response.headers.get("Retry-After"))
                raise ModelError("model_rate_limited", retryable=True, retry_after=delay) from exc
            raise ModelError("model_request_failed", retryable=exc.status_code >= 500) from exc
        if response.status != "completed" or not response.output_text:
            raise ModelError("model_no_result", retryable=False)
        try:
            return schema.model_validate_json(response.output_text), response.model
        except ValidationError as exc:
            raise ModelError("model_invalid_result", retryable=True) from exc


def build_host(*, local: bool = False) -> InvocationAgentServerHost:
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
    os.environ["AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED"] = "false"
    deployment = os.environ["MODEL_DEPLOYMENT_NAME"]
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    version = os.environ.get("FOUNDRY_AGENT_VERSION", "local-dev")
    timeouts = MathTimeouts()

    @asynccontextmanager
    async def connect(*, timeout: int, job_id: str, operation: str):
        credential = AzureCliCredential() if local else ManagedIdentityCredential()
        async with credential, AIProjectClient(endpoint=endpoint, credential=credential) as project:
            async with project.get_openai_client(timeout=timeout, max_retries=0) as client:
                yield FoundryModel(client, deployment, job_id=job_id, operation=operation)

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    host = InvocationAgentServerHost(configure_observability=None)

    @host.invoke_handler
    async def invoke(request: Request) -> JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_BODY:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body.extend(chunk)
        try:
            payload = _REQUEST.validate_json(body)
        except (ValidationError, AgentInputError):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        raw_job_id = request.headers.get(JOB_ID_HEADER, "")
        try:
            job_id = str(UUID(raw_job_id)) if len(raw_job_id) == 36 else "-"
        except ValueError:
            job_id = "-"
        math_v2 = isinstance(payload, AnalyzePage) and payload.version == "2"
        started = monotonic()
        try:
            async with connect(
                timeout=timeouts.model_timeout_seconds if math_v2 else 45,
                operation=payload.operation, job_id=job_id,
            ) as model:
                page_timeout = timeouts.page_budget(request.headers.get(PAGE_READ_BUDGET_HEADER))
                result = await MathAgent(model, version, page_timeout=page_timeout).invoke(payload)
        except AgentInputError:
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        except ModelError as exc:
            logger.warning(
                "agent_invocation_failed code=%s retryable=%s job_id=%s operation=%s elapsed_ms=%d",
                exc.code, exc.retryable, job_id, payload.operation, (monotonic() - started) * 1000,
            )
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
            return JSONResponse({"error": exc.code}, status_code=503 if exc.retryable else 424, headers=headers)
        if isinstance(result, AnalysisResult) and any(item.review_timed_out for item in result.items):
            logger.warning("open_review_pending job_id=%s operation=%s elapsed_ms=%d",
                           job_id, payload.operation, (monotonic() - started) * 1000)
        if isinstance(payload, AnalyzePage) and payload.version == "1":
            return JSONResponse(result.model_dump(mode="json", exclude={
                "items": {"__all__": {"assessment": True, "source": {"question": True}}},
            }))
        return JSONResponse(result.model_dump(mode="json"))

    return host


if __name__ == "__main__":
    local = os.environ.get("EDU_AGENT_MODE") == "local"
    build_host(local=local).run(host="127.0.0.1" if local else "0.0.0.0", port=8088)
