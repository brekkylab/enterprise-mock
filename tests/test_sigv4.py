import pytest

from app import synth
from app.acl import Acl, Caller
from app.sigv4 import expected_signature, parse_authorization, split_credential

botocore = pytest.importorskip("botocore")
from botocore.auth import S3SigV4Auth            # noqa: E402
from botocore.awsrequest import AWSRequest       # noqa: E402
from botocore.credentials import Credentials     # noqa: E402


TOKEN = "usr-7d0022af43df72b74a89"
AK = synth.s3_access_key_id(TOKEN)
SK = synth.s3_secret_access_key(TOKEN)


def _sign(method, url, region="us-east-1"):
    """Sign a request exactly as boto3 would; return (headers, path, query)."""
    from urllib.parse import urlsplit
    req = AWSRequest(method=method, url=url)
    req.headers["x-amz-content-sha256"] = "UNSIGNED-PAYLOAD"
    S3SigV4Auth(Credentials(AK, SK), "s3", region).add_auth(req)
    parts = urlsplit(url)
    headers = dict(req.headers)
    # A bare AWSRequest never gets a Host header (real HTTP clients add it at the wire
    # layer, not on the request object) but botocore's signer still folds it into the
    # canonical request via the URL. A real request arriving over HTTP always carries
    # Host, so reproduce that here rather than skip verifying it.
    headers.setdefault("host", parts.netloc)
    return headers, parts.path, parts.query


def _verify(headers, method, path, query):
    hdrs = {k.lower(): v for k, v in headers.items()}
    parsed = parse_authorization(hdrs["authorization"])
    ak, date_stamp, region = split_credential(parsed["credential"])
    assert ak == AK
    return expected_signature(
        SK, method, path, query, hdrs, parsed["signed_headers"],
        hdrs.get("x-amz-content-sha256", "UNSIGNED-PAYLOAD"),
        hdrs["x-amz-date"], date_stamp, region), parsed["signature"]


def test_verifier_accepts_a_real_botocore_signature():
    headers, path, query = _sign("GET", "http://127.0.0.1:8000/s3/eng-artifacts?list-type=2")
    expected, provided = _verify(headers, "GET", path, query)
    assert expected == provided


def test_verifier_accepts_a_signed_object_get():
    headers, path, query = _sign("GET", "http://127.0.0.1:8000/s3/eng-artifacts/runbooks/oncall.md")
    expected, provided = _verify(headers, "GET", path, query)
    assert expected == provided


def test_verifier_rejects_a_tampered_signature():
    headers, path, query = _sign("GET", "http://127.0.0.1:8000/s3/eng-artifacts/runbooks/oncall.md")
    expected, provided = _verify(headers, "GET", path, "list-type=2")  # query changed after signing
    assert expected != provided


def test_acl_resolve_access_key(tmp_path):
    import yaml
    tokens = tmp_path / "tokens.yaml"
    tokens.write_text(yaml.safe_dump({
        "admin_token": "admin-service-token",
        "users": [{"email": "ava@acme.com", "name": "Ava", "token": TOKEN}],
    }))
    acl = Acl.load(tokens, "admin-service-token", "acme")
    caller, secret = acl.resolve_access_key(AK)
    assert caller == Caller(email="ava@acme.com", is_admin=False) and secret == SK
    admin_caller, admin_secret = acl.resolve_access_key(synth.s3_access_key_id("admin-service-token"))
    assert admin_caller.is_admin and admin_secret == synth.s3_secret_access_key("admin-service-token")
    assert acl.resolve_access_key("AKIADOESNOTEXIST0000") is None
