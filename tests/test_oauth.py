"""Mock OAuth credentials: generation + the runtime resolver (app.oauth)."""
import jwt
import pytest

from app import oauth
from app.config import Settings


@pytest.fixture
def creds(tmp_path):
    s = Settings(data_dir=tmp_path, org_name="acme")
    oauth.generate(s, org="acme")
    return s, oauth.Oauth.load(s.credentials_path)


def test_generate_writes_credentials(creds):
    s, o = creds
    assert s.credentials_path.exists()
    assert o is not None and o._data["org"] == "acme"
    # one shared OAuth client + one service account with a real private key; no per-user data
    assert o.client_config()["client_id"].endswith(".apps.googleusercontent.com")
    assert "BEGIN PRIVATE KEY" in o.service_account_json("http://x")["private_key"]
    assert "users" not in o._data


def _assertion(o, claims):
    sa = o.service_account_json("http://x/oauth2/token")
    return jwt.encode({"iss": sa["client_email"], "aud": sa["token_uri"],
                       "iat": 0, "exp": 9_999_999_999, **claims}, sa["private_key"], algorithm="RS256")


def test_service_account_assertion(creds):
    _, o = creds
    # domain-wide delegation: sub selects the impersonated user
    assert o.verify_assertion(_assertion(o, {"sub": "bob@acme.com"})) == "bob@acme.com"
    # bare service account (no sub) → sentinel so the endpoint grants a service identity
    assert o.verify_assertion(_assertion(o, {})) == ("", "sa")
    # wrong issuer / garbage signature → rejected
    assert o.verify_assertion(_assertion(o, {"iss": "evil@x", "sub": "bob@acme.com"})) is None
    assert o.verify_assertion("not.a.jwt") is None


def test_public_key_not_exposed(creds):
    _, o = creds
    # the SA bundle handed out carries the private key (client signs) but never the public key
    assert "public_key_pem" not in o.service_account_json("http://x")
