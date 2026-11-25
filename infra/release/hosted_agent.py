import argparse
import json
import re
import time
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import ContainerConfiguration, HostedAgentDefinition, ProtocolVersionRecord
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / "test.settings.json").read_text())
ENDPOINT = f"https://{SETTINGS['foundry']}.services.ai.azure.com/api/projects/{SETTINGS['project']}"
AGENT = SETTINGS["agent"]


def definition(image: str) -> HostedAgentDefinition:
    registry = re.escape(SETTINGS["registry"])
    if not re.fullmatch(rf"{registry}(?:-[a-z0-9]+)?\.azurecr\.io/edu-agent@sha256:[0-9a-f]{{64}}", image):
        raise ValueError("Expected the test registry's digest-pinned edu-agent image")
    config = HostedAgentDefinition(
        cpu="1",
        memory="2Gi",
        container_configuration=ContainerConfiguration(image=image),
        protocol_versions=[ProtocolVersionRecord(protocol="invocations", version="2.0.0")],
        environment_variables={
            "MODEL_DEPLOYMENT_NAME": SETTINGS["model"],
            "PYTHONNOUSERSITE": "1",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
            "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED": "false",
        },
    )
    config["session_configuration"] = {"idle_timeout_seconds": 120}
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the test Hosted Agent from a verified image.")
    parser.add_argument("image")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = definition(args.image)
    if args.dry_run:
        print(json.dumps(config.as_dict(), indent=2))
        return

    with (
        AzureCliCredential() as credential,
        AIProjectClient(endpoint=ENDPOINT, credential=credential) as project,
    ):
        try:
            latest = project.agents.get(AGENT).versions.latest
        except ResourceNotFoundError:
            latest = None
        if latest and latest.metadata.get("image") == args.image:
            if latest.definition.as_dict() != config.as_dict():
                raise RuntimeError(
                    "Existing agent metadata matches the image but not the expected definition"
                )
            version = latest.version
        else:
            version = project.agents.create_version(
                AGENT, definition=config, metadata={"image": args.image}
            ).version

        for _ in range(90):
            record = project.agents.get_version(AGENT, version)
            if record.status == "active":
                break
            if record.status != "creating":
                raise RuntimeError(f"Agent version {version} has status {record.status}")
            time.sleep(10)
        else:
            raise TimeoutError(f"Agent version {version} did not become active")
        active = project.agents.get(AGENT)
        if active.state != "enabled" or active.versions.latest.version != version:
            raise RuntimeError("Agent endpoint is not enabled on the expected release")
        print(
            json.dumps(
                {
                    "version": version,
                    "agentUrl": f"{ENDPOINT}/agents/{AGENT}/endpoint/protocols/invocations",
                }
            )
        )


if __name__ == "__main__":
    main()
