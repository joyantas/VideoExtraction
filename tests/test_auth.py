import time
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from videoextration.auth import EntraAuth


@pytest.fixture
def auth(monkeypatch):
    tenant = str(uuid4())
    monkeypatch.setenv("AZURE_TENANT_ID", tenant)
    monkeypatch.setenv("AZURE_API_AUDIENCE", "sample-api")
    verifier = EntraAuth()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier.keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    claims = {
        "iss": verifier.issuer, "aud": "sample-api", "tid": tenant, "oid": str(uuid4()),
        "iat": int(time.time()), "nbf": int(time.time()) - 1, "exp": int(time.time()) + 300,
        "scp": "Video.Analyze",
    }
    return verifier, key, claims


def test_valid_delegated_access_token(auth):
    verifier, key, claims = auth
    token = jwt.encode(claims, key, algorithm="RS256")
    assert verifier.authenticate(f"Bearer {token}") == f"{claims['tid']}:{claims['oid']}"


@pytest.mark.parametrize("field,value", [
    ("iss", "https://attacker.example"),
    ("aud", "wrong-api"),
    ("exp", 1),
    ("nbf", 4_000_000_000),
])
def test_invalid_token_claims_rejected(auth, field, value):
    verifier, key, claims = auth
    claims[field] = value
    with pytest.raises(HTTPException) as error:
        verifier.authenticate("Bearer " + jwt.encode(claims, key, algorithm="RS256"))
    assert error.value.status_code == 401


def test_id_token_without_api_scope_rejected(auth):
    verifier, key, claims = auth
    del claims["scp"]
    with pytest.raises(HTTPException) as error:
        verifier.authenticate("Bearer " + jwt.encode(claims, key, algorithm="RS256"))
    assert error.value.status_code == 403
