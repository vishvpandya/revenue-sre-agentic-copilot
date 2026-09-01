"""Password hashing and opaque session-token helpers for synthetic demo tenants."""

# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Synthetic local demo: keeps login responsive while retaining a salted, memory-hard hash.
# A deployed production service would use an environment-reviewed work factor.
_N = 2**12
_R = 8
_P = 1
_DKLEN = 32


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password using stdlib scrypt; plaintext is never stored."""

    if not password:
        raise ValueError("password must not be empty")
    actual_salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=actual_salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return "scrypt${}${}${}${}${}".format(
        _N,
        _R,
        _P,
        base64.urlsafe_b64encode(actual_salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if algorithm != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt_b64.encode("ascii")),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.urlsafe_b64decode(digest_b64.encode("ascii"))),
        )
        return hmac.compare_digest(candidate, base64.urlsafe_b64decode(digest_b64.encode("ascii")))
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
