import logging
import os
from contextlib import asynccontextmanager

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from edu_agent.service import AgentInputError, MathAgent, ModelError, model_schema
from edu_domain.contracts import MAX_PAGE_BYTES, AgentRequest

logger = logging.getLogger(__name__)
_REQUEST = TypeAdapter(AgentRequest)
_MAX_BODY = ((MAX_PAGE_BYTES + 2) // 3) * 4 + 4096


class FoundryModel:
    def __init__(self, client: AsyncOpenAI, deployment: str):
        self.client = client
        self.deployment = deployment

    async def infer[T: BaseModel](
        self, schema: type[T], instructions: str, text: str, *, image: str | None = None
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
        except (APIConnectionError, ClientAuthenticationError) as exc:
            raise ModelError("model_unreachable", retryable=True) from exc
        except APIStatusError as exc:
            raise ModelError(
                "model_request_failed", retryable=exc.status_code in (408, 429) or exc.status_code >= 500
            ) from exc
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

    @asynccontextmanager
    async def connect():
        credential = AzureCliCredential() if local else ManagedIdentityCredential()
        async with credential, AIProjectClient(endpoint=endpoint, credential=credential) as project:
            async with project.get_openai_client(timeout=45.0, max_retries=0) as client:
                yield MathAgent(FoundryModel(client, deployment), version)

    host = InvocationAgentServerHost(configure_observability=None) if local else InvocationAgentServerHost()

    @host.invoke_handler
    async def invoke(request: Request) -> JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_BODY:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body.extend(chunk)
        try:
            payload = _REQUEST.validate_json(body)
            async with connect() as agent:
                result = await agent.invoke(payload)
        except (ValidationError, AgentInputError):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        except ModelError as exc:
            logger.warning("model_call_failed code=%s retryable=%s", exc.code, exc.retryable)
            return JSONResponse({"error": exc.code}, status_code=503 if exc.retryable else 424)
        return JSONResponse(result.model_dump(mode="json"))

    return host


if __name__ == "__main__":
    local = os.environ.get("EDU_AGENT_MODE") == "local"
    build_host(local=local).run(host="127.0.0.1" if local else "0.0.0.0", port=8088)
