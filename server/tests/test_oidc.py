"""OIDC の公開鍵のキャッシュと、本物の RSA 鍵で署名したトークンの検証。"""

import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from google.auth import crypt, jwt
from starlette.requests import Request

from sky_server.gcp import oidc
from sky_server.gcp.oidc import (
    CertsCache,
    GoogleTokenVerifier,
    OidcTaskAuthenticator,
    fetch_google_certs,
)
from sky_server.weather.common import MAX_RESPONSE_BYTES, RetryableError

AUDIENCE = "https://sky.example.run.app/internal/tasks/fetch-weather"
EMAIL = "tasks@proj.iam.gserviceaccount.com"


def make_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def public_pem(key) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


KEY = make_key()
OTHER_KEY = make_key()


def make_token(key=KEY, kid="k1", omit=(), **overrides) -> str:
    """`omit` に挙げた claim は入れない。"""
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": EMAIL,
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
        **overrides,
    }
    for name in omit:
        del claims[name]
    signer = crypt.RSASigner.from_string(private_pem(key), kid)
    return jwt.encode(signer, claims, header={"kid": kid}).decode()


class FakeFetch:
    """公開鍵の取得関数の偽物。呼ばれた回数を数え、`certs` を返す。`error` があれば投げる。"""

    def __init__(self, certs=None, error=None) -> None:
        self.certs = {"k1": public_pem(KEY)} if certs is None else certs
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.certs


class FakeTime:
    """秒を数える時計。`now` を書き換えて進める。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_verifier(fetch, clock=None) -> GoogleTokenVerifier:
    return GoogleTokenVerifier(CertsCache(fetch, clock or FakeTime()))


def make_request(token: str) -> Request:
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})


def authenticate(token: str, fetch=None) -> None:
    verifier = make_verifier(fetch or FakeFetch())
    OidcTaskAuthenticator(AUDIENCE, EMAIL, verifier)(make_request(token))


def assert_denied(token: str, fetch=None) -> None:
    with pytest.raises(HTTPException) as e:
        authenticate(token, fetch)
    assert e.value.status_code == 401


def test_正しい鍵で署名したトークンは通る():
    authenticate(make_token())


def test_公開鍵はキャッシュされ2回目は取得しない():
    fetch = FakeFetch()
    verifier = make_verifier(fetch)
    assert verifier(make_token(), AUDIENCE)["email"] == EMAIL
    assert verifier(make_token(), AUDIENCE)["email"] == EMAIL
    assert fetch.calls == 1


def test_公開鍵は1時間たつと取り直す():
    fetch = FakeFetch()
    clock = FakeTime()
    verifier = make_verifier(fetch, clock)
    verifier(make_token(), AUDIENCE)
    clock.now += 3599
    verifier(make_token(), AUDIENCE)
    assert fetch.calls == 1
    clock.now += 1
    verifier(make_token(), AUDIENCE)
    assert fetch.calls == 2


def test_知らないkidなら取り直すが60秒以内の2回目は取り直さない():
    fetch = FakeFetch()
    clock = FakeTime()
    verifier = make_verifier(fetch, clock)
    verifier(make_token(), AUDIENCE)
    # 取得から 60 秒以内は、知らない kid でも取り直さない
    clock.now += 59
    with pytest.raises(ValueError):
        verifier(make_token(kid="k2"), AUDIENCE)
    assert fetch.calls == 1
    # 60 秒たてば取り直す。鍵が入れ替わっていれば、新しい kid で通る
    clock.now += 1
    fetch.certs = {"k1": public_pem(KEY), "k2": public_pem(OTHER_KEY)}
    assert verifier(make_token(OTHER_KEY, kid="k2"), AUDIENCE)["email"] == EMAIL
    assert fetch.calls == 2
    # 取り直した直後は、また知らない kid が来ても取り直さない
    clock.now += 10
    with pytest.raises(ValueError):
        verifier(make_token(kid="k3"), AUDIENCE)
    assert fetch.calls == 2


def test_取得に失敗した直後の60秒以内は取りに行かない():
    fetch = FakeFetch(error=ConnectionError("down"))
    clock = FakeTime()
    verifier = make_verifier(fetch, clock)
    for _ in range(3):
        with pytest.raises(Exception):  # noqa: B017
            verifier(make_token(), AUDIENCE)
    assert fetch.calls == 1
    clock.now += 60
    fetch.error = None
    assert verifier(make_token(), AUDIENCE)["email"] == EMAIL
    assert fetch.calls == 2


@pytest.mark.parametrize("token", ["abc", "a.b", "a.b.c.d"])
def test_3つの部分でないトークンは取得関数を呼ばずに401(token):
    fetch = FakeFetch()
    assert_denied(token, fetch)
    assert fetch.calls == 0


def test_3つの部分でも中身が壊れていれば401():
    assert_denied("a.b.c")


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.example.com"},
        {"aud": "https://other.example.com"},
        {"email": "other@example.com"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"exp": int(time.time()) - 3600},
    ],
)
def test_claimsが違えば401(overrides):
    assert_denied(make_token(**overrides))


@pytest.mark.parametrize("name", ["iss", "aud", "email", "email_verified"])
def test_claimsがなければ401(name):
    assert_denied(make_token(omit=(name,)))


def test_issは2つの書き方のどちらも通る():
    authenticate(make_token(iss="accounts.google.com"))
    authenticate(make_token(iss="https://accounts.google.com"))


def test_別の鍵で署名したトークンは401():
    assert_denied(make_token(OTHER_KEY))


def test_kidのないトークンは取得関数を呼ばずに401():
    now = int(time.time())
    signer = crypt.RSASigner.from_string(private_pem(KEY))
    token = jwt.encode(signer, {"iss": "accounts.google.com", "aud": AUDIENCE, "exp": now + 60})
    fetch = FakeFetch()
    assert_denied(token.decode(), fetch)
    assert fetch.calls == 0


def test_公開鍵の取得に失敗したら401():
    assert_denied(make_token(), FakeFetch(error=RetryableError("HTTP 503")))


def test_公開鍵の取得に失敗した理由はログにだけ出る(caplog):
    with pytest.raises(HTTPException) as e:
        authenticate(make_token(), FakeFetch(error=RetryableError("HTTP 503")))
    assert "HTTP 503" in caplog.text
    assert "HTTP 503" not in e.value.detail


# 本物の取得関数（通信は偽物のトランスポートで差し替える）


def patch_client(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        oidc, "create_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_公開鍵の取得は応答のJSONを返す(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"k1": "PEM"})

    patch_client(monkeypatch, handler)
    assert fetch_google_certs() == {"k1": "PEM"}
    assert seen == ["https://www.googleapis.com/oauth2/v1/certs"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json=["k1"]),
        httpx.Response(200, json={"k1": 1}),
    ],
)
def test_公開鍵の取得の失敗は例外になる(monkeypatch, response):
    patch_client(monkeypatch, lambda request: response)
    with pytest.raises(Exception):  # noqa: B017
        fetch_google_certs()


def test_公開鍵の応答が5MBを超えたら取得に失敗する(monkeypatch):
    body = b" " * (MAX_RESPONSE_BYTES + 1)
    patch_client(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(RetryableError, match="大きすぎる"):
        fetch_google_certs()
