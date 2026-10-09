"""Cloud Tasks が付ける OIDC トークンの検証（M4 の GCP 版）。"""

import logging
from collections.abc import Callable

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

# トークンと audience を受け取り、検証できたら claims を返す。失敗したら例外を投げる
Verifier = Callable[[str, str], dict]


def verify_google_token(token: str, audience: str) -> dict:
    """署名、有効期限、発行者が Google であること、audience を確かめる。"""
    return id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)


class OidcTaskAuthenticator:
    """`Authorization: Bearer <ID トークン>` を検証して、呼び出し元が Cloud Tasks か確かめる。"""

    def __init__(self, audience: str, email: str, verify: Verifier | None = None) -> None:
        self._audience = audience
        self._email = email
        self._verify = verify or verify_google_token

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
