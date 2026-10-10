"""撮影者とメタデータ（schema_version 1）のモデル。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

# 小文字に正規化する前の UUID の形式（ハイフン付きの36文字）
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"

# これより前の撮影時刻は受け付けない（年が4桁にならない時刻や、UTC に直すとあふれる時刻を避ける）
MIN_CAPTURED_AT = datetime(2020, 1, 1, tzinfo=UTC)

# サーバー時刻よりこれ以上未来の撮影時刻は受け付けない
MAX_FUTURE = timedelta(minutes=10)

Float = Annotated[float, Field(allow_inf_nan=False)]


def to_utc_millis(t: datetime) -> str:
    """時刻を UTC のミリ秒までの文字列（例 `2026-10-09T03:00:00.123Z`）にする。

    文字列のまま並べると時刻順になる。
    """
    utc = t.astimezone(UTC)
    return f"{utc.year:04d}-{utc:%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


class PrivacyZone(BaseModel):
    """公開版を作るときに隠す範囲（中心と半径）。サーバーが観測を受け取るときには使わない。"""

    zone_id: str
    lat: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
    lon: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
    radius_m: Annotated[float, Field(gt=0, le=50000, allow_inf_nan=False)]
    label: str | None = None
    created_at: datetime


class User(BaseModel):
    user_id: str
    name: str
    token_hash: str
    created_at: datetime
    revoked_at: datetime | None = None
    consent_public: bool = False
    privacy_zones: list[PrivacyZone] = []
    # delete-user を始めた時刻と、そのとき消した観測の ID（2回目以降の削除で使う）
    deletion_started_at: datetime | None = None
    deletion_observation_ids: list[str] = []


class _Strict(BaseModel):
    # 知らない項目は 422 にして、つづりの間違いに気づけるようにする
    model_config = ConfigDict(extra="forbid")


class Location(_Strict):
    lat: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
    lon: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
    accuracy_m: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    altitude_m: Float | None
    fix_time: AwareDatetime


class Orientation(_Strict):
    azimuth_deg: Annotated[float, Field(ge=0, lt=360, allow_inf_nan=False)] | None
    pitch_deg: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)] | None
    roll_deg: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)] | None
    declination_deg: Float | None
    accuracy: Literal["high", "medium", "low", "unreliable"] | None
    stddev_deg: Float | None


class Camera(_Strict):
    focal_length_mm: Float | None
    focal_length_35mm: Float | None
    exposure_time_s: Float | None
    iso: int | None
    f_number: Float | None
    white_balance: str | None
    image_width: int | None
    image_height: int | None


class Device(_Strict):
    platform: Literal["android", "ios", "web"]
    os_version: str
    model: str
    app_version: str


class OrientationTrace(_Strict):
    source: Annotated[str, Field(min_length=1, max_length=200)]
    # 同じ添字が1件のサンプル。Firestore は配列の中に配列を入れられないので、項目ごとの配列にする
    t_sensor_ms: Annotated[list[Float], Field(max_length=1000)]
    alpha: Annotated[list[Float], Field(max_length=1000)]
    beta: Annotated[list[Float], Field(max_length=1000)]
    gamma: Annotated[list[Float], Field(max_length=1000)]

    @model_validator(mode="after")
    def _same_length(self) -> "OrientationTrace":
        lengths = {len(self.t_sensor_ms), len(self.alpha), len(self.beta), len(self.gamma)}
        if len(lengths) != 1:
            raise ValueError("t_sensor_ms・alpha・beta・gamma の長さがそろっていない")
        return self


class Capture(_Strict):
    pressed_at: AwareDatetime
    completed_at: AwareDatetime
    duration_ms: Annotated[int, Field(ge=0, le=600000)]
    motion_deg: Annotated[float, Field(ge=0, le=180, allow_inf_nan=False)] | None
    sensor_clock_offset_ms: Float | None
    exif_datetime_original: Annotated[str, Field(max_length=64)] | None
    exif_subsec_time_original: Annotated[str, Field(max_length=64)] | None
    orientation_trace: OrientationTrace | None

    @model_validator(mode="after")
    def _completed_after_pressed(self) -> "Capture":
        if self.completed_at < self.pressed_at:
            raise ValueError("completed_at が pressed_at より前になっている")
        return self


class ObservationMetadata(_Strict):
    schema_version: Literal[1]
    observation_id: Annotated[str, Field(pattern=UUID_PATTERN)]
    captured_at: AwareDatetime
    tz_offset_min: int
    location: Location | None
    orientation: Orientation | None
    camera: Camera
    device: Device
    capture_path: Literal["native", "web"]
    user_guess: Literal["rain", "no_rain"] | None
    image_sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]
    # 古いアプリは送らないので、キーがなくてもよい唯一の項目
    capture: Capture | None = None

    @field_validator("observation_id", "image_sha256")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()

    @field_validator("captured_at")
    @classmethod
    def _within_range(cls, value: datetime) -> datetime:
        if value < MIN_CAPTURED_AT:
            raise ValueError("captured_at が 2020-01-01T00:00:00Z より前になっている")
        if value > datetime.now(UTC) + MAX_FUTURE:
            raise ValueError("captured_at がサーバー時刻より10分以上未来になっている")
        return value

    def with_server_fields(self, user_id: str, received_at: datetime, image_key: str) -> dict:
        return {
            **self.model_dump(mode="json"),
            "user_id": user_id,
            "captured_at_utc": to_utc_millis(self.captured_at),
            "received_at": received_at.isoformat(),
            "image_key": image_key,
        }
