"""内部 API（Cloud Tasks からの呼び出し）の認証。"""

import os
from collections.abc import Callable

from fastapi import HTTPException, Request

# Request を受け取り、認証に失敗したら 401 を投げる
TaskAuthenticator = Callable[[Request], None]


def deny_all(request: Request) -> None:
    raise HTTPException(status_code=401, detail="認証に失敗しました")


def allow_all(request: Request) -> None:
    return None


def get_task_authenticator() -> TaskAuthenticator:
    """設定 `SKY_TASK_AUTH` から選ぶ。未設定ならすべて拒否、`none` ならすべて許可。"""
    mode = os.environ.get("SKY_TASK_AUTH")
    if mode is None:
        return deny_all
    if mode == "none":
        return allow_all
    raise ValueError(f"SKY_TASK_AUTH の値が正しくありません: {mode!r}（使えるのは none だけです）")
