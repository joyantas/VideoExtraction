"""Single-tenant Entra access-token validation for Azure mode."""

import os
from uuid import UUID

import jwt
from fastapi import HTTPException


class EntraAuth:
    def __init__(self):
        tenant = str(UUID(os.environ["AZURE_TENANT_ID"]))
        self.audience = os.environ["AZURE_API_AUDIENCE"]
        self.scope = os.getenv("AZURE_REQUIRED_SCOPE", "Video.Analyze")
        self.issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
            cache_keys=True, lifespan=3600, timeout=10,
        )

    def authenticate(self, authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "A bearer access token is required.")
        token = authorization[7:]
        if not token or len(token) > 16_384:
            raise HTTPException(401, "Invalid access token.")
        try:
            signing_key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, signing_key.key, algorithms=["RS256"],
                audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "oid", "tid"]},
                leeway=30,
            )
            if self.scope not in str(claims.get("scp", "")).split():
                raise HTTPException(403, "The access token lacks the required delegated API scope.")
            return f"{claims['tid']}:{claims['oid']}"
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
            raise HTTPException(401, "Access-token validation failed.") from exc
