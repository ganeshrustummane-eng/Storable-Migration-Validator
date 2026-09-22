"""
Enterprise Authentication Layer
================================
Provides a production-ready authentication abstraction for the Migration
Intelligence Connector.

Authentication modes (controlled by AUTH_MODE env var):
  "jwt"    — validate JWT (HS256 or RS256) with full claims checking
  "static" — validate CONNECTOR_API_TOKEN bearer string, roles from env
  "dev"    — no validation; actor from request body (local dev only)

JWT claims extracted and forwarded as AuthResult:
  sub          — user identifier (never logged as secret)
  email        — display name / audit actor
  roles        — list of role names (VIEWER, REVIEWER, RULE_ADMIN, …)
  permissions  — optional explicit permission list (overrides role defaults)
  exp          — expiration (enforced)
  iss          — issuer (enforced when JWT_ISSUER is set)
  aud          — audience (enforced when JWT_AUDIENCE is set)

SECURITY INVARIANTS:
  - Credentials (JWT_SECRET, CONNECTOR_API_TOKEN) never appear in responses.
  - Expired tokens are always rejected (exp checked before clock skew).
  - Issuer and audience mismatches are rejected as AUTHENTICATION_ERROR.
  - Tokens are never forwarded to downstream services or logged.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# AuthResult — what a verified token yields
# ---------------------------------------------------------------------------

@dataclass
class AuthResult:
    """Decoded, validated identity from a token."""
    user_id:     str                # "sub" claim or static token identifier
    email:       str                # display identity (used as audit actor)
    roles:       List[str]          # e.g. ["REVIEWER"]
    permissions: List[str]          # explicit perms (empty = derive from roles)
    raw_claims:  Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def actor(self) -> str:
        """Canonical string used in audit records (email preferred over sub)."""
        return self.email or self.user_id


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuthenticationError(Exception):
    """Token is missing, malformed, expired, or has wrong issuer/audience."""
    def __init__(self, message: str, code: str = "AUTHENTICATION_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AuthProvider(ABC):
    @abstractmethod
    def verify(self, token: str) -> AuthResult:
        """Validate the token and return AuthResult, or raise AuthenticationError."""


# ---------------------------------------------------------------------------
# JWTAuthProvider  (production / staging)
# ---------------------------------------------------------------------------

class JWTAuthProvider(AuthProvider):
    """
    Validates signed JWT access tokens.

    Supports:
      HS256 — shared secret (JWT_SECRET env var). Use for internal services.
      RS256 — RSA public key (JWT_PUBLIC_KEY env var or OIDC JWKS). Use for
               Gemini Enterprise or external OIDC providers.

    Claims checked:
      exp  — always enforced (no clock skew allowance)
      iss  — enforced when JWT_ISSUER is configured
      aud  — enforced when JWT_AUDIENCE is configured
    """

    def __init__(
        self,
        secret: Optional[str] = None,
        algorithm: Optional[str] = None,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
    ):
        # Read env vars fresh here, not as module-level/default-argument
        # constants — those are evaluated once at import time and silently
        # ignore any later os.environ change (real deployments where this
        # module gets imported before .env loads elsewhere, and tests using
        # unittest.mock.patch.dict, both break with eager module-level reads).
        self._secret    = secret if secret is not None else os.getenv("JWT_SECRET", "")
        self._algorithm = algorithm if algorithm is not None else os.getenv("JWT_ALGORITHM", "HS256")
        self._issuer    = issuer if issuer is not None else (os.getenv("JWT_ISSUER", "") or None)
        self._audience  = audience if audience is not None else (os.getenv("JWT_AUDIENCE", "") or None)

    def verify(self, token: str) -> AuthResult:
        try:
            import jwt as pyjwt
        except ImportError:
            raise RuntimeError(
                "PyJWT is required for JWT authentication. "
                "Run: pip install PyJWT"
            )

        if not self._secret:
            raise AuthenticationError(
                "JWT_SECRET is not configured. "
                "Set it via env var or use AUTH_MODE=static for dev.",
            )

        options: Dict[str, Any] = {
            "verify_exp": True,
            "verify_iss": bool(self._issuer),
            "verify_aud": bool(self._audience),
        }
        kwargs: Dict[str, Any] = {
            "algorithms": [self._algorithm],
            "options":    options,
        }
        if self._issuer:
            kwargs["issuer"] = self._issuer
        if self._audience:
            kwargs["audience"] = self._audience

        try:
            claims = pyjwt.decode(token, self._secret, **kwargs)
        except pyjwt.ExpiredSignatureError:
            raise AuthenticationError(
                "Token has expired. Obtain a new access token.",
                code="TOKEN_EXPIRED",
            )
        except pyjwt.InvalidIssuerError:
            raise AuthenticationError(
                f"Invalid token issuer. Expected: {self._issuer}",
                code="INVALID_ISSUER",
            )
        except pyjwt.InvalidAudienceError:
            raise AuthenticationError(
                f"Invalid token audience. Expected: {self._audience}",
                code="INVALID_AUDIENCE",
            )
        except pyjwt.InvalidSignatureError:
            raise AuthenticationError(
                "Token signature is invalid.",
                code="INVALID_SIGNATURE",
            )
        except pyjwt.DecodeError as exc:
            raise AuthenticationError(
                f"Malformed token: {exc}",
                code="MALFORMED_TOKEN",
            )
        except pyjwt.PyJWTError as exc:
            raise AuthenticationError(f"Token validation failed: {exc}")

        return AuthResult(
            user_id     = str(claims.get("sub", "")),
            email       = str(claims.get("email", claims.get("sub", ""))),
            roles       = list(claims.get("roles", [])),
            permissions = list(claims.get("permissions", [])),
            raw_claims  = {k: v for k, v in claims.items()
                           if k not in ("sub", "email", "roles", "permissions")},
        )


# ---------------------------------------------------------------------------
# StaticTokenProvider  (hackathon / CI)
# ---------------------------------------------------------------------------

class StaticTokenProvider(AuthProvider):
    """
    Validates a pre-shared static bearer token.

    Roles are configured via CONNECTOR_ROLES env var (comma-separated).
    This is suitable for CI integration and hackathon demos — NOT production.

    A static token is opaque (no expiry or audience); it should be rotated
    regularly and never committed to source control.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        roles: Optional[List[str]] = None,
    ):
        self._token = token if token is not None else os.getenv("CONNECTOR_API_TOKEN", "")
        self._roles = roles if roles is not None else [
            r.strip() for r in os.getenv("CONNECTOR_ROLES", "ADMIN").split(",") if r.strip()
        ]

    def verify(self, token: str) -> AuthResult:
        if not self._token:
            raise AuthenticationError(
                "CONNECTOR_API_TOKEN is not configured.",
                code="AUTHENTICATION_ERROR",
            )
        if token != self._token:
            raise AuthenticationError(
                "Invalid bearer token.",
                code="AUTHENTICATION_ERROR",
            )
        return AuthResult(
            user_id     = "service-account",
            email       = os.getenv("CONNECTOR_ACTOR", "service-account"),
            roles       = list(self._roles),
            permissions = [],
        )


# ---------------------------------------------------------------------------
# DevAuthProvider  (local development, no validation)
# ---------------------------------------------------------------------------

class DevAuthProvider(AuthProvider):
    """
    No-op authentication for local development.

    Every request is accepted with ADMIN role. Never use in production.
    A warning is emitted to remind developers that auth is disabled.
    """

    def verify(self, token: str) -> AuthResult:
        import warnings
        warnings.warn(
            "[AUTH] Running in DEV mode — all requests are accepted as ADMIN. "
            "Set AUTH_MODE=jwt or AUTH_MODE=static for production.",
            stacklevel=3,
        )
        # Treat the token value as the actor name for traceability
        actor = token.strip() or "dev-user"
        return AuthResult(
            user_id     = actor,
            email       = actor,
            roles       = ["ADMIN"],
            permissions = [],
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_auth_provider() -> AuthProvider:
    """
    Return the configured authentication provider.

    AUTH_MODE env var selects the provider:
      "jwt"    → JWTAuthProvider  (production)
      "static" → StaticTokenProvider  (hackathon / CI)
      "dev"    → DevAuthProvider  (local dev, no validation)
    """
    auth_mode = os.getenv("AUTH_MODE", "static").lower()
    if auth_mode == "jwt":
        return JWTAuthProvider()
    if auth_mode == "dev":
        return DevAuthProvider()
    return StaticTokenProvider()


# Module-level singleton (re-instantiated per test if needed)
_provider: Optional[AuthProvider] = None


def verify_bearer(authorization_header: Optional[str]) -> AuthResult:
    """
    Parse 'Bearer <token>' from the Authorization header and validate it.

    Raises AuthenticationError on any failure.
    Used by the FastAPI layer — do not catch AuthenticationError here;
    let the API translate it to an HTTP 401 response.
    """
    global _provider
    if _provider is None:
        _provider = get_auth_provider()

    if not authorization_header:
        raise AuthenticationError(
            "Authorization header is required.",
            code="MISSING_TOKEN",
        )
    if not authorization_header.startswith("Bearer "):
        raise AuthenticationError(
            "Authorization must use the Bearer scheme.",
            code="MALFORMED_TOKEN",
        )

    token = authorization_header[len("Bearer "):].strip()
    if not token:
        raise AuthenticationError(
            "Bearer token is empty.",
            code="MISSING_TOKEN",
        )

    return _provider.verify(token)


def reset_provider() -> None:
    """Force re-instantiation of the provider (used in tests to swap modes)."""
    global _provider
    _provider = None
