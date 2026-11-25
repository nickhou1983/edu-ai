from uuid import UUID

from fastapi import HTTPException, Request

from edu_app.config import Settings

DEMO_STUDENTS = ("demo-a", "demo-b")


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.mode == "azure":
            import jwt

            tenant = settings.entra_tenant_id
            self.jwt = jwt
            self.issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
            self.keys = jwt.PyJWKClient(
                f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", timeout=5
            )

    def public_config(self) -> dict:
        if self.settings.mode == "local":
            return {
                "mode": "local",
                "demoStudents": list(DEMO_STUDENTS),
                "sampleOnly": self.settings.agent_url is None,
            }
        return {
            "mode": "entra",
            "tenantId": str(self.settings.entra_tenant_id),
            "clientId": str(self.settings.entra_spa_client_id),
            "scope": f"api://{self.settings.entra_api_client_id}/access_as_user",
        }

    def current_owner(self, request: Request) -> str:
        if self.settings.mode == "local":
            student = request.headers.get("x-demo-student")
            if student in DEMO_STUDENTS:
                return student
            raise HTTPException(401, "请选择一个本地演示账号。")

        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        unauthenticated = HTTPException(401, "请重新登录后再继续。", headers={"WWW-Authenticate": "Bearer"})
        if scheme.lower() != "bearer" or not token or len(token) > 8192:
            raise unauthenticated
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = self.jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=str(self.settings.entra_api_client_id),
                options={"require": ["exp", "iat", "iss", "aud", "tid", "oid", "azp", "scp"]},
            )
            tenant = UUID(claims["tid"])
            user = UUID(claims["oid"])
        except self.jwt.PyJWKClientConnectionError as exc:
            raise HTTPException(503, "登录服务暂不可用，请稍后重试。") from exc
        except (
            self.jwt.InvalidTokenError,
            self.jwt.PyJWKClientError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            raise unauthenticated from exc
        if (
            tenant != self.settings.entra_tenant_id
            or claims["azp"] != str(self.settings.entra_spa_client_id)
            or not isinstance(claims["scp"], str)
            or "access_as_user" not in claims["scp"].split()
        ):
            raise HTTPException(403, "此账号无权使用应用。")
        request.state.edu_roles = claims.get("roles", [])
        return f"{tenant}:{user}"

    def can_manage_knowledge(self, request: Request, owner: str) -> bool:
        if self.settings.mode == "local":
            return owner == "demo-a"
        roles = getattr(request.state, "edu_roles", [])
        return isinstance(roles, list) and any(role in ("Teacher", "Admin") for role in roles)

    def knowledge_manager(self, request: Request) -> str:
        owner = self.current_owner(request)
        if not self.can_manage_knowledge(request, owner):
            raise HTTPException(403, "只有教师或管理员可以维护共享知识点。")
        return owner
