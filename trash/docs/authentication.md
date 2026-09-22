# Authentication

## Overview

The Migration Validator connector supports three authentication modes, selected via the `AUTH_MODE` environment variable.

## Modes

### Static Token (Default)

Best for: hackathon demo, CI/CD pipelines, single-team deployments.

```bash
AUTH_MODE=static
CONNECTOR_API_TOKEN=your-secret-token
CONNECTOR_ROLES=REVIEWER,RULE_ADMIN
```

Client sends:
```
Authorization: Bearer your-secret-token
```

All requests authenticated with this token receive the roles listed in `CONNECTOR_ROLES`.

---

### JWT (Enterprise)

Best for: enterprise SSO integration, multi-user deployments.

```bash
AUTH_MODE=jwt
JWT_SECRET=your-hmac-secret-256-bit
JWT_ISSUER=https://your-idp.company.com
JWT_AUDIENCE=migration-validator
JWT_ALGORITHM=HS256
```

Client sends a signed JWT:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9...
```

**Expected JWT claims:**

| Claim | Required | Description |
|-------|----------|-------------|
| `sub` | Yes | User ID |
| `email` | Recommended | User email (used as actor) |
| `exp` | Yes | Expiry timestamp |
| `iss` | Yes | Must match `JWT_ISSUER` |
| `aud` | Yes | Must match `JWT_AUDIENCE` |
| `roles` | Yes | List of role strings |
| `allowed_tables` | No | List of allowed table names (null = all) |
| `permissions` | No | Explicit permission override list |

**Example JWT payload:**
```json
{
  "sub": "user-456",
  "email": "analyst@company.com",
  "iss": "https://your-idp.company.com",
  "aud": "migration-validator",
  "exp": 1724700000,
  "roles": ["REVIEWER"],
  "allowed_tables": ["customer", "orders"]
}
```

---

### Dev Mode (Local Development Only)

```bash
AUTH_MODE=dev
```

No token required. All requests treated as ADMIN. Emits a warning on startup. **Never use in production.**

---

## Public Endpoints

These endpoints do not require authentication:

- `GET /health`
- `GET /tools`

---

## Generating a Static Token

```python
import secrets
token = secrets.token_urlsafe(32)
print(token)
# Store in .env as CONNECTOR_API_TOKEN
```

---

## Generating a Test JWT

```python
import jwt
from datetime import datetime, timedelta

payload = {
    "sub": "test-user-123",
    "email": "test@company.com",
    "iss": "https://your-idp.company.com",
    "aud": "migration-validator",
    "exp": datetime.utcnow() + timedelta(hours=24),
    "roles": ["REVIEWER"]
}

token = jwt.encode(payload, "your-secret", algorithm="HS256")
print(f"Authorization: Bearer {token}")
```
