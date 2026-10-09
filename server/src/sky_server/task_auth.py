"""内部 API（Cloud Tasks からの呼び出し）の認証。"""

import os
from collections.abc import Callable

from fastapi import HTTPException, Request

from sky_server.config import get_gcp_settings

# Request を受け取り、認証に失敗したら 401 を投げる
TaskAuthenticator = Callable[[Request], None]


def deny_all(request: Request) -> None:
    raise HTTPException(status_code=401, detail="認証に失敗しました")


def allow_all(request: Request) -> None:
    return None


def get_task_authenticator() -> TaskAuthenticator:
    """設定 `SKY_TASK_AUTH` から選ぶ。

    未設定ならすべて拒否、`none` ならすべて許可、
    `oidc` なら Cloud Tasks の OIDC トークンを検証する。
    """
    mode = os.environ.get("SKY_TASK_AUTH")
    if mode is None:
        return deny_all
    if mode == "none":
        return allow_all
    if mode == "oidc":
        # google.auth は oidc のときだけ読み込む
        from sky_server.gcp.oidc import OidcTaskAuthenticator

        settings = get_gcp_settings()
        return OidcTaskAuthenticator(settings.tasks_target_url, settings.tasks_service_account)
    raise ValueError(
        f"SKY_TASK_AUTH の値が正しくありません: {mode!r}（使えるのは none と oidc です）"
    )
