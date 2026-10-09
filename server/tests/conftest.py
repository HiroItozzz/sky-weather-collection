import copy
import hashlib
import json
import sys
import types
from datetime import UTC, datetime, timedelta
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


def fixed_clock(t: datetime):
    """いつも同じ時刻を返す時計。"""
    return lambda: t


class SteppingClock:
    """呼ばれるたびに `step` ずつ進む時計。呼ばれた回数は `calls` に残る。"""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._next = start
        self._step = step
        self.calls = 0

    def __call__(self) -> datetime:
        now = self._next
        self._next += self._step
        self.calls += 1
        return now


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


WEATHER_AT_CAPTURE = {
    "category": "cloudy",
    "weather_code": 3,
    "temperature_c": 18.2,
    "precipitation_mm": 0.0,
    "cloud_cover_pct": 90,
}
RAIN_ANSWER = {"result": "rain", "source": "amedas"}


class FakeSummary(types.ModuleType):
    """`sky_server.summary` の偽物。計算はせず、決まった値を返して、呼ばれ方を `calls` に残す。

    `error` に例外を入れると、`weather_at_capture` と `answer` がそれを投げる。
    """

    def __init__(self) -> None:
        super().__init__("sky_server.summary")
        self.weather = WEATHER_AT_CAPTURE
        self.result = RAIN_ANSWER
        self.error: Exception | None = None
        self.calls: list[tuple] = []

    def weather_at_capture(self, open_meteo_envelope, captured_at):
        self.calls.append(("weather_at_capture", open_meteo_envelope, captured_at))
        if self.error:
            raise self.error
        return self.weather

    def answer(self, open_meteo_envelope, amedas_envelope, captured_at):
        self.calls.append(("answer", open_meteo_envelope, amedas_envelope, captured_at))
        if self.error:
            raise self.error
        return self.result

    def is_correct(self, user_guess, answer):
        if user_guess is None or answer is None or answer["result"] == "unknown":
            return None
        return user_guess == answer["result"]


@pytest.fixture
def fake_summary(monkeypatch) -> FakeSummary:
    """要約の計算を偽物に差し替える。本物の `sky_server.summary` があってもなくても動く。"""
    fake = FakeSummary()
    monkeypatch.setitem(sys.modules, "sky_server.summary", fake)
    return fake
