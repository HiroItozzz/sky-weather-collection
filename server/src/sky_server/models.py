"""撮影者とメタデータ（schema_version 1）のモデル。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

# 小文字に正規化する前の UUID の形式（ハイフン付きの36文字）
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"

# サーバー時刻よりこれ以上未来の撮影時刻は受け付けない
MAX_FUTURE = timedelta(minutes=10)

Float = Annotated[float, Field(allow_inf_nan=False)]


class User(BaseModel):
    user_id: str
    name: str
    token_hash: str
    created_at: datetime
    revoked_at: datetime | None = None
    consent_public: bool = False


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

    @field_validator("observation_id", "image_sha256")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()

    @field_validator("captured_at")
    @classmethod
    def _not_future(cls, value: datetime) -> datetime:
        if value > datetime.now(UTC) + MAX_FUTURE:
            raise ValueError("captured_at がサーバー時刻より10分以上未来になっている")
        return value

    def with_server_fields(self, user_id: str, received_at: datetime, image_key: str) -> dict:
        return {
            **self.model_dump(mode="json"),
            "user_id": user_id,
            "received_at": received_at.isoformat(),
            "image_key": image_key,
        }
