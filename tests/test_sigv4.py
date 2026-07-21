from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import pytest
from starlette.requests import Request

from app import auth, synth
from app.acl import Acl, Caller
from app.sigv4 import (
    expected_signature,
    is_skewed,
    parse_amz_date,
    parse_authorization,
    split_credential,
)

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


# ---------------------------------------------------------------- request-time fidelity
# real S3 rejects header-auth requests whose x-amz-date has drifted more than 15
# minutes from the server clock (RequestTimeTooSkewed), and rejects presigned URLs once
# X-Amz-Date + X-Amz-Expires has elapsed (AccessDenied). These tests build self-consistent
# requests (signed via `expected_signature` with the real derived secret) so they're
# deterministic regardless of wall-clock — no dependency on when the suite happens to run.

AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"


def _acl():
    return Acl({TOKEN: "ava@acme.com"}, "admin-service-token", "acme")


def _request(method, path, query, headers) -> Request:
    """A minimal Starlette Request mirroring what `resolve_sigv4` reads: headers,
    query_params, method, url.query, and scope['raw_path'] — plus a fake app.state.acl
    so `auth.acl(request)` resolves without a real ASGI app."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": [(k.lower().encode("ascii"), v.encode("ascii")) for k, v in headers.items()],
        "scheme": "http",
        "server": ("mock", 80),
        "app": SimpleNamespace(state=SimpleNamespace(acl=_acl())),
    }
    return Request(scope)


def _header_auth_request(amz_date: str, path="/s3/eng-artifacts", query="list-type=2",
                         region="us-east-1"):
    """Build a header-auth GET signed for `amz_date` with a genuinely valid signature."""
    date_stamp = amz_date[:8]
    signed_headers = "host;x-amz-date"
    headers = {"host": "mock", "x-amz-date": amz_date, "x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
    sig = expected_signature(SK, "GET", path, query, headers, signed_headers,
                             "UNSIGNED-PAYLOAD", amz_date, date_stamp, region)
    credential = f"{AK}/{date_stamp}/{region}/s3/aws4_request"
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={credential}, "
                                f"SignedHeaders={signed_headers}, Signature={sig}")
    return _request("GET", path, query, headers)


def _presigned_request(amz_date: str, expires: int, path="/s3/eng-artifacts", region="us-east-1"):
    """Build a presigned-query GET signed for `amz_date`/`expires` with a valid signature."""
    date_stamp = amz_date[:8]
    signed_headers = "host"
    headers = {"host": "mock"}
    credential = f"{AK}/{date_stamp}/{region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": signed_headers,
    }
    query = urlencode(params, safe="-_.~", quote_via=quote)
    sig = expected_signature(SK, "GET", path, query, headers, signed_headers,
                             "UNSIGNED-PAYLOAD", amz_date, date_stamp, region)
    query = f"{query}&X-Amz-Signature={sig}"
    return _request("GET", path, query, headers)


def test_parse_amz_date_and_is_skewed_are_pure():
    now = datetime.now(timezone.utc)
    assert parse_amz_date("garbage") is None
    assert parse_amz_date("") is None
    parsed = parse_amz_date("20260101T000000Z")
    assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not is_skewed(now, now)
    assert not is_skewed(now - timedelta(minutes=14), now)
    assert is_skewed(now - timedelta(minutes=16), now)
    assert is_skewed(now + timedelta(minutes=16), now)  # skew is bidirectional


def test_header_auth_rejects_skewed_date():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    req = _header_auth_request(stale)
    caller, err = auth.resolve_sigv4(req)
    assert caller is None
    assert err == "RequestTimeTooSkewed"


def test_header_auth_skew_check_precedes_signature_check():
    # A stale date with a BROKEN signature must still report RequestTimeTooSkewed — proving the
    # time check runs BEFORE signature verification (a signature-first order would instead return
    # SignatureDoesNotMatch). The access key is valid, so key-lookup passes and the time check wins.
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    date_stamp = stale[:8]
    signed_headers = "host;x-amz-date"
    headers = {"host": "mock", "x-amz-date": stale, "x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
    credential = f"{AK}/{date_stamp}/us-east-1/s3/aws4_request"
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={credential}, "
                                f"SignedHeaders={signed_headers}, Signature=deadbeef")
    caller, err = auth.resolve_sigv4(_request("GET", "/s3/eng-artifacts", "list-type=2", headers))
    assert caller is None
    assert err == "RequestTimeTooSkewed"


def test_header_auth_accepts_current_date():
    current = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _header_auth_request(current)
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)


def test_presigned_expired_is_access_denied():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(stale, expires=60)
    caller, err = auth.resolve_sigv4(req)
    assert caller is None
    assert err == "AccessDenied"


def test_presigned_unexpired_ok():
    current = datetime.now(timezone.utc).strftime(AMZ_DATE_FORMAT)
    req = _presigned_request(current, expires=3600)
    caller, err = auth.resolve_sigv4(req)
    assert err is None
    assert caller == Caller(email="ava@acme.com", is_admin=False)
