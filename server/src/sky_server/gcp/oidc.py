"""Cloud Tasks が付ける OIDC トークンの検証（M4 の GCP 版）。"""

import logging
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request
from google.auth import jwt

from sky_server.weather.common import Monotonic, create_client, read_limited

logger = logging.getLogger(__name__)

CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
# 公開鍵をキャッシュしておく時間（秒）
CERTS_TTL_S = 3600.0
# 公開鍵を取りに行く間隔の下限（秒）。知らない kid を並べて送られても通信が増えないようにする
CERTS_MIN_INTERVAL_S = 60.0
# 時計のずれとして許す秒数
CLOCK_SKEW_S = 10
ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# トークンと audience を受け取り、検証できたら claims を返す。失敗したら例外を投げる
Verifier = Callable[[str, str], dict]
# 公開鍵（kid から PEM への対応表）を取得する関数。失敗したら例外を投げる
CertsFetcher = Callable[[], dict[str, str]]


def fetch_google_certs() -> dict[str, str]:
    """Google の公開鍵を取得する。応答の大きさとタイムアウトは外部 API と同じ扱い。"""
    with create_client() as client:
        response = read_limited(client, CERTS_URL, {})
    if not 200 <= response.status_code < 300:
        raise ValueError(f"公開鍵の取得に失敗しました: HTTP {response.status_code}")
    certs = response.json()
    if not isinstance(certs, dict) or not all(
        isinstance(kid, str) and isinstance(pem, str) for kid, pem in certs.items()
    ):
        raise ValueError("公開鍵の応答の形が正しくありません")
    return certs


class CertsCache:
    """公開鍵のキャッシュ。1時間で取り直し、知らない kid での取り直しは 60 秒に1回まで。"""

    def __init__(
        self,
        fetch: CertsFetcher = fetch_google_certs,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._certs: dict[str, str] | None = None
        self._fetched_at = 0.0
        # 取得を試みた時刻。失敗したときも記録し、失敗が続く間も通信が増えないようにする
        self._last_attempt: float | None = None

    def get(self, kid: str) -> dict[str, str]:
        """`kid` を含むはずの公開鍵の対応表を返す。キャッシュにも取り直しにも `kid` がなければ、
        含まないまま返す（署名の検証が失敗する）。取得に失敗したら例外を投げる。
        """
        with self._lock:
            now = self._monotonic()
            expired = self._certs is None or now - self._fetched_at >= CERTS_TTL_S
            unknown = not expired and kid not in self._certs
            if expired or unknown:
                recent = self._last_attempt is not None and (
                    now - self._last_attempt < CERTS_MIN_INTERVAL_S
                )
                if not recent:
                    self._last_attempt = now
                    self._certs = self._fetch()
                    self._fetched_at = now
                elif expired:
                    raise ValueError("公開鍵が古く、取り直しも間隔の下限の内側のため使えません")
            return self._certs


class GoogleTokenVerifier:
    """Google が発行した ID トークンを、キャッシュした公開鍵で検証する。

    署名、有効期限、audience を `google.auth.jwt.decode` で確かめ、発行者（`iss`）が
    Google であることを自分で確かめる（`verify_oauth2_token` がしていることと同じ）。
    """

    def __init__(self, certs: CertsCache | None = None) -> None:
        self._certs = certs or CertsCache()

    def __call__(self, token: str, audience: str) -> dict:
        # 公開鍵を取りに行く前に、形の違うトークンを断る
        if len(token.split(".")) != 3:
            raise ValueError("トークンが3つの部分に分かれていません")
        kid = jwt.decode_header(token).get("kid")
        if not isinstance(kid, str):
            raise ValueError("トークンのヘッダーに kid がありません")
        # 発行した側とこのサーバーの時計のずれで、発行直後のトークンを断らないよう少し許す
        claims = jwt.decode(
            token,
            certs=self._certs.get(kid),
            audience=audience,
            clock_skew_in_seconds=CLOCK_SKEW_S,
        )
        if claims.get("iss") not in ISSUERS:
            raise ValueError(f"発行者が Google ではありません: {claims.get('iss')!r}")
        return claims


class OidcTaskAuthenticator:
    """`Authorization: Bearer <ID トークン>` を検証して、呼び出し元が Cloud Tasks か確かめる。"""

    def __init__(self, audience: str, email: str, verify: Verifier | None = None) -> None:
        self._audience = audience
        self._email = email
        self._verify = verify or GoogleTokenVerifier()

    def __call__(self, request: Request) -> None:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            self._deny("Authorization ヘッダーがありません")
        try:
            claims = self._verify(token.strip(), self._audience)
        except Exception as e:
            self._deny(f"トークンの検証に失敗しました: {type(e).__name__}: {e}")
        if claims.get("email") != self._email:
            self._deny("email が設定と一致しません")
        if claims.get("email_verified") is not True:
            self._deny("email_verified が true ではありません")

    @staticmethod
    def _deny(reason: str) -> None:
        # 理由はログにだけ出し、応答には出さない
        logger.warning("内部 API の認証に失敗しました: %s", reason)
        raise HTTPException(status_code=401, detail="認証に失敗しました")
