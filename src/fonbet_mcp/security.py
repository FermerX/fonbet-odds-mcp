"""Optional OAuth 2.1 resource-server gate for ChatGPT's MCP connector.

The identity provider handles user authentication and the authorization-code
flow. This module *only* verifies signed bearer JWTs issued by that provider.
Without MCP_AUTH_MODE=oauth the existing public connector is unchanged.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKClient, PyJWTError
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl


def _https_url(value: str, key: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{key} must be a valid public HTTPS URL")
    if parsed.fragment:
        raise ValueError(f"{key} must not contain a fragment")
    return value


def _scopes(claims: dict[str, Any]) -> list[str]:
    scope = claims.get("scope")
    if isinstance(scope, str):
        return [item for item in scope.split() if item]
    scp = claims.get("scp")
    if isinstance(scp, str):
        return [item for item in scp.split() if item]
    if isinstance(scp, list) and all(isinstance(item, str) for item in scp):
        return list(scp)
    return []


class JWKSJWTVerifier:
    """Verify provider-signed JWTs; reject expired, altered or wrong-audience tokens."""

    def __init__(self, issuer: str, jwks_url: str, audience: str, required_scopes: list[str], allowed_subject: str):
        self.issuer = issuer
        self.audience = audience
        self.required_scopes = set(required_scopes)
        self.allowed_subject = allowed_subject
        # PyJWKClient caches JWKS and signing keys, fetching via TLS. No secret
        # or client API key is stored in this server or in GitHub.
        self.jwks = PyJWKClient(
            jwks_url, cache_keys=True, lifespan=3600, timeout=5
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iss", "aud", "sub"]},
                leeway=30,
            )
            scopes = _scopes(claims)
            if not self.required_scopes.issubset(scopes):
                return None
            subject = claims["sub"]
            if not isinstance(subject, str) or subject != self.allowed_subject:
                return None
            client_id = claims.get("client_id") or claims.get("azp") or subject
            if not isinstance(client_id, str) or not client_id:
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(claims["exp"]),
                resource=self.audience,
                subject=subject,
                claims={"iss": self.issuer},
            )
        except (PyJWTError, ValueError, TypeError, KeyError, OSError):
            # Never log tokens or include them in error messages.
            return None


def build_auth_kwargs(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Configure opt-in strict OAuth; invalid opt-in configuration fails closed."""
    env = os.environ if environ is None else environ
    mode = env.get("MCP_AUTH_MODE", "none").strip().lower()
    if mode == "none":
        return {}
    if mode != "oauth":
        raise ValueError("MCP_AUTH_MODE must be 'none' or 'oauth'")

    required = ("MCP_OAUTH_ISSUER", "MCP_OAUTH_RESOURCE_URL", "MCP_OAUTH_JWKS_URL", "MCP_OAUTH_ALLOWED_SUBJECT")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ValueError("OAuth is enabled but these settings are missing: " + ", ".join(missing))

    issuer = _https_url(env["MCP_OAUTH_ISSUER"].strip(), "MCP_OAUTH_ISSUER")
    resource_url = _https_url(env["MCP_OAUTH_RESOURCE_URL"].strip(), "MCP_OAUTH_RESOURCE_URL")
    jwks_url = _https_url(env["MCP_OAUTH_JWKS_URL"].strip(), "MCP_OAUTH_JWKS_URL")
    audience = env.get("MCP_OAUTH_AUDIENCE", resource_url).strip()
    if not audience:
        raise ValueError("MCP_OAUTH_AUDIENCE must not be empty")
    allowed_subject = env["MCP_OAUTH_ALLOWED_SUBJECT"].strip()
    if not allowed_subject:
        raise ValueError("MCP_OAUTH_ALLOWED_SUBJECT must not be empty")
    scopes = env.get("MCP_OAUTH_REQUIRED_SCOPES", "fonbet:read").split()
    if not scopes:
        raise ValueError("MCP_OAUTH_REQUIRED_SCOPES must have at least one scope")
    # Authorization-request scopes may include provider-specific audience hints.
    # Such hints need not appear as scopes in the issued access token, so keep
    # requested scopes separate from the scopes enforced by the JWT verifier.
    request_scopes = env.get("MCP_OAUTH_REQUEST_SCOPES", " ".join(scopes)).split()
    if not set(scopes).issubset(request_scopes):
        raise ValueError("MCP_OAUTH_REQUEST_SCOPES must include all required scopes")

    verifier = JWKSJWTVerifier(
        issuer=issuer, jwks_url=jwks_url,
        audience=audience, required_scopes=scopes,
        allowed_subject=allowed_subject,
    )
    return {
        # The verifier validates the 'aud' claim even if the identity
        # provider uses an API audience different from resource_url.
        "token_verifier": verifier,
        "auth": AuthSettings(
            issuer_url=AnyHttpUrl(issuer),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=request_scopes,
            validate_token_resource=False,
        ),
    }
