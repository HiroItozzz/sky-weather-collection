import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = "./data"


def get_data_dir() -> Path:
    return Path(os.environ.get("SKY_DATA_DIR", DEFAULT_DATA_DIR))


DEFAULT_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_AMEDAS_INTERVAL_S = 1.0


def get_amedas_enabled() -> bool:
    """アメダスを取得するか。`SKY_AMEDAS_ENABLED=0` のときだけ無効にする。"""
    return os.environ.get("SKY_AMEDAS_ENABLED", "1") != "0"


def get_amedas_interval_s() -> float:
    """アメダスの呼び出しの間隔（秒）。"""
    return float(os.environ.get("SKY_AMEDAS_INTERVAL_S", DEFAULT_AMEDAS_INTERVAL_S))


def get_open_meteo_url() -> str:
    return os.environ.get("SKY_OPEN_METEO_URL", DEFAULT_OPEN_METEO_URL)


DEFAULT_DAILY_UPLOAD_LIMIT = 100


class ConfigError(ValueError):
    """設定の値が正しくない、または必須の設定が足りないときのエラー。"""


def get_daily_upload_limit() -> int:
    """撮影者ごとの1日（UTC）の新規の観測の上限。1以上の整数でなければエラー。"""
    text = os.environ.get("SKY_DAILY_UPLOAD_LIMIT")
    if text is None:
        return DEFAULT_DAILY_UPLOAD_LIMIT
    try:
        limit = int(text)
    except ValueError:
        limit = 0
    if limit < 1:
        raise ConfigError(
            f"SKY_DAILY_UPLOAD_LIMIT の値が正しくありません: {text!r}（1以上の整数にしてください）"
        )
    return limit


BACKENDS = ("local", "gcp")
DEFAULT_TASKS_LOCATION = "us-central1"
DEFAULT_TASKS_QUEUE = "weather-fetch"


def get_backend_name() -> str:
    """使う版。`SKY_BACKEND` の `local`（既定）か `gcp`。それ以外はエラー。"""
    name = os.environ.get("SKY_BACKEND", "local")
    if name not in BACKENDS:
        raise ConfigError(
            f"SKY_BACKEND の値が正しくありません: {name!r}（使えるのは local と gcp です）"
        )
    return name


@dataclass(frozen=True)
class GcpSettings:
    project: str
    bucket: str
    tasks_location: str
    tasks_queue: str
    tasks_target_url: str
    tasks_service_account: str


def get_gcp_settings() -> GcpSettings:
    """GCP 版の設定を読む。必須の値が足りなければ、足りない名前をすべて示してエラーにする。"""
    required = {
        "project": "SKY_GCP_PROJECT",
        "bucket": "SKY_GCS_BUCKET",
        "tasks_target_url": "SKY_TASKS_TARGET_URL",
        "tasks_service_account": "SKY_TASKS_SERVICE_ACCOUNT",
    }
    values = {key: os.environ.get(name, "") for key, name in required.items()}
    missing = [name for key, name in required.items() if not values[key]]
    if missing:
        raise ConfigError(f"GCP 版に必要な設定が足りません: {', '.join(missing)}")
    return GcpSettings(
        tasks_location=os.environ.get("SKY_TASKS_LOCATION") or DEFAULT_TASKS_LOCATION,
        tasks_queue=os.environ.get("SKY_TASKS_QUEUE") or DEFAULT_TASKS_QUEUE,
        **values,
    )
