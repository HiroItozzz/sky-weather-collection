import os
from pathlib import Path

DEFAULT_DATA_DIR = "./data"


def get_data_dir() -> Path:
    return Path(os.environ.get("SKY_DATA_DIR", DEFAULT_DATA_DIR))
