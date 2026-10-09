import os
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
