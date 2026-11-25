import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / "test.settings.json").read_text())
GRAPH = "https://graph.microsoft.com/v1.0"
DEFAULT_ROLE = "00000000-0000-0000-0000-000000000000"


def az(*args):
    result = subprocess.run(
        ["az", *args, "--only-show-errors", "-o", "json"], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def graph(method, path, body=None):
    if not path.startswith("/"):
        raise ValueError("Expected a relative Graph API path")
    body_arg = ("--headers", "Content-Type=application/json", "--body", json.dumps(body)) if body else ()
    return az("rest", "--method", method, "--url", GRAPH + path, *body_arg)


def collection(path, **query):
    result = graph("GET", f"{path}?{urlencode(query)}")
    if result.get("@odata.nextLink"):
        raise RuntimeError(f"Unexpected extra Graph results for {path}")
    return result["value"]


def find(kind, filter_by):
    return collection(f"/{kind}", **{"$filter": filter_by, "$select": "id,appId"})


def verify_users(users: list[str]) -> None:
    account = az("account", "show")
    if account["id"] != SETTINGS["subscriptionId"] or account["tenantId"] != SETTINGS["tenantId"]:
        raise RuntimeError("Azure CLI is not using the approved subscription and tenant")
    if len(set(users)) != 2:
        raise ValueError("Nominate two distinct test users")
    for user in users:
        record = graph("GET", f"/users/{user}?$select=id,accountEnabled")
        if record["id"] != user or not record["accountEnabled"]:
            raise RuntimeError("A nominated user is not active in the test tenant")


def redirect_uri(origin: str) -> str:
    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or origin != f"https://{url.hostname}"
        or not re.fullmatch(
            rf"{re.escape(SETTINGS['api'])}\.[a-z0-9.-]+\.azurecontainerapps\.io", url.hostname
        )
    ):
        raise ValueError("Expected the dedicated test Container App HTTPS origin")
    return f"{origin}/auth-redirect.html"


def verify_assignments(app, users):
    (sp,) = find("servicePrincipals", f"appId eq '{app['appId']}'")
    record = graph("GET", f"/servicePrincipals/{sp['id']}?$select=appRoleAssignmentRequired")
    if record["appRoleAssignmentRequired"] is not True:
        raise RuntimeError("Test enterprise application does not require assignment")
    assignments = collection(
        f"/servicePrincipals/{sp['id']}/appRoleAssignedTo",
        **{"$select": "principalId,principalType,appRoleId"},
    )
    actual = {(a["principalId"], a["principalType"], a["appRoleId"]) for a in assignments}
    if actual != {(user, "User", DEFAULT_ROLE) for user in users}:
        raise RuntimeError("Test enterprise application assignments do not match the two nominated users")


def release(users, uri, *, create=False):
    api_name, spa_name = SETTINGS["apiRegistration"], SETTINGS["spaRegistration"]
    existing = [find("applications", f"displayName eq '{name}'") for name in (api_name, spa_name)]
    if create:
        if any(existing):
            raise RuntimeError(
                "A test application name already exists; inspect the existing registrations first"
            )
        scope_id = str(uuid4())
        scope = {
            "id": scope_id,
            "isEnabled": True,
            "value": "access_as_user",
            "type": "Admin",
            "adminConsentDisplayName": "Use the Edu AI synthetic test API",
            "adminConsentDescription": "Access the synthetic test API on behalf of an assigned user.",
        }
        api = graph(
            "POST",
            "/applications",
            {
                "displayName": api_name,
                "signInAudience": "AzureADMyOrg",
                "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": [scope]},
            },
        )
        api_object, api_client = api["id"], api["appId"]
        graph("PATCH", f"/applications/{api_object}", {"identifierUris": [f"api://{api_client}"]})
        api_sp = graph("POST", "/servicePrincipals", {"appId": api_client, "appRoleAssignmentRequired": True})
        spa = graph(
            "POST",
            "/applications",
            {
                "displayName": spa_name,
                "signInAudience": "AzureADMyOrg",
                "spa": {"redirectUris": [uri]},
                "requiredResourceAccess": [
                    {"resourceAppId": api_client, "resourceAccess": [{"id": scope_id, "type": "Scope"}]}
                ],
            },
        )
        spa_sp = graph(
            "POST", "/servicePrincipals", {"appId": spa["appId"], "appRoleAssignmentRequired": True}
        )
        for sp in (api_sp, spa_sp):
            for user in users:
                graph(
                    "POST",
                    f"/servicePrincipals/{sp['id']}/appRoleAssignedTo",
                    {
                        "principalId": user,
                        "resourceId": sp["id"],
                        "appRoleId": DEFAULT_ROLE,
                    },
                )
        graph(
            "PATCH",
            f"/applications/{api_object}",
            {
                "api": {
                    "requestedAccessTokenVersion": 2,
                    "oauth2PermissionScopes": [scope],
                    "preAuthorizedApplications": [
                        {"appId": spa["appId"], "delegatedPermissionIds": [scope_id]}
                    ],
                },
            },
        )
    else:
        (api,), (spa,) = existing

    api = graph("GET", f"/applications/{api['id']}?$select=id,appId,api,identifierUris,signInAudience")
    spa = graph("GET", f"/applications/{spa['id']}?$select=id,appId,spa,signInAudience")
    policy = api["api"]
    (scope,) = policy["oauth2PermissionScopes"]
    if (
        api["signInAudience"] != "AzureADMyOrg"
        or spa["signInAudience"] != "AzureADMyOrg"
        or spa["spa"]["redirectUris"] != [uri]
        or api["identifierUris"] != [f"api://{api['appId']}"]
        or policy["requestedAccessTokenVersion"] != 2
        or scope["value"] != "access_as_user"
        or scope["isEnabled"] is not True
        or scope["type"] != "Admin"
        or policy["preAuthorizedApplications"]
        != [{"appId": spa["appId"], "delegatedPermissionIds": [scope["id"]]}]
    ):
        raise RuntimeError("Test application configuration does not match the expected single-tenant policy")
    for app in (api, spa):
        verify_assignments(app, users)
    print(
        json.dumps(
            {
                role: {"appId": app["appId"], "objectId": app["id"]}
                for role, app in (("api", api), ("spa", spa))
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure or verify two-user test access.")
    parser.add_argument("action", choices=("check-users", "create", "verify"))
    parser.add_argument("users", nargs=2, type=lambda value: str(UUID(value)), metavar="USER_OBJECT_ID")
    parser.add_argument("--origin")
    args = parser.parse_args()
    if args.action != "check-users" and not args.origin:
        parser.error("--origin is required for create and verify")
    try:
        verify_users(args.users)
        if args.action != "check-users":
            release(args.users, redirect_uri(args.origin), create=args.action == "create")
        else:
            print("Two nominated test users are active in the approved tenant.")
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip() or f"Azure CLI failed with status {exc.returncode}") from None


if __name__ == "__main__":
    main()
