import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sky_server.admin import create_user
from sky_server.main import create_app
from sky_server.storage import LocalObservationRepository

OBSERVATION_ID = "123e4567-e89b-42d3-a456-426614174000"
IMAGE = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def make_metadata(image: bytes = IMAGE, observation_id: str = OBSERVATION_ID) -> dict:
    return {
        "schema_version": 1,
        "observation_id": observation_id,
        "captured_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "tz_offset_min": 540,
        "location": {
            "lat": 35.68,
            "lon": 139.76,
            "accuracy_m": 8.5,
            "altitude_m": None,
            "fix_time": datetime.now(UTC).isoformat(),
        },
        "orientation": {
            "azimuth_deg": 123.4,
            "pitch_deg": 45.0,
            "roll_deg": 1.5,
            "declination_deg": -7.5,
            "accuracy": "high",
            "stddev_deg": 0.8,
        },
        "camera": {
            "focal_length_mm": 5.4,
            "focal_length_35mm": 24.0,
            "exposure_time_s": 0.001,
            "iso": 50,
            "f_number": 1.8,
            "white_balance": None,
            "image_width": 4000,
            "image_height": 3000,
        },
        "device": {
            "platform": "android",
            "os_version": "15",
            "model": "Pixel 8",
            "app_version": "0.1.0",
        },
        "capture_path": "native",
        "user_guess": None,
        "image_sha256": hashlib.sha256(image).hexdigest(),
    }


class Api:
    def __init__(self, client: TestClient, repository: LocalObservationRepository) -> None:
        self.client = client
        self.repository = repository

    def new_user(self, name: str = "tester") -> tuple[str, str]:
        user, token = create_user(self.repository, name)
        return user.user_id, token

    def put(
        self,
        token: str | None,
        metadata: dict | None = None,
        image: bytes = IMAGE,
        observation_id: str = OBSERVATION_ID,
        content_type: str = "image/jpeg",
    ):
        if metadata is None:
            metadata = make_metadata(image, observation_id)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.put(
            f"/v1/observations/{observation_id}",
            headers=headers,
            data={"metadata": json.dumps(metadata)},
            files={"image": ("photo.jpg", image, content_type)},
        )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def api(data_dir: Path) -> Api:
    app = create_app(data_dir)
    return Api(TestClient(app), LocalObservationRepository(data_dir))


@pytest.fixture
def token(api: Api) -> str:
    return api.new_user()[1]


@pytest.fixture
def metadata() -> dict:
    return copy.deepcopy(make_metadata())
