"""管理用コマンド：撮影者の作成・無効化・削除、プライバシーゾーンと公開への同意、天気ジョブの実行。"""

import argparse
import sys
import uuid
from datetime import UTC, datetime

from sky_server.auth import generate_token, hash_token
from sky_server.backends import Backend, build_backend
from sky_server.config import ConfigError, get_backend_name, get_data_dir
from sky_server.jobs import (
    PHASES,
    JobRepository,
    JobRunner,
    LocalTaskScheduler,
    default_fetchers,
)
from sky_server.models import PrivacyZone, User
from sky_server.storage import BlobStore, ObservationRepository
from sky_server.weather.common import Clock, raw_key, utc_now

# 天気の生レスポンスの保存先を探すときの提供元（ジョブが残っていなくても消せるようにする）
RAW_PROVIDERS = ("open_meteo", "amedas")


def create_user(repository: ObservationRepository, name: str) -> tuple[User, str]:
    token = generate_token()
    user = User(
        user_id=str(uuid.uuid4()),
        name=name,
        token_hash=hash_token(token),
        created_at=datetime.now(UTC),
    )
    repository.add_user(user)
    return user, token


def find_user(repository: ObservationRepository, user_id: str) -> User | None:
    """撮影者を探す。UUID の形でなければ、保存先に問い合わせずに None を返す。"""
    try:
        uuid.UUID(user_id)
    except ValueError:
        return None
    return repository.get_user(user_id)


def revoke_user(repository: ObservationRepository, user_id: str) -> User | None:
    user = find_user(repository, user_id)
    if user is None:
        return None
    if user.revoked_at is None:
        user = user.model_copy(update={"revoked_at": datetime.now(UTC)})
        repository.update_user(user)
    return user


def add_privacy_zone(
    repository: ObservationRepository,
    user: User,
    lat: float,
    lon: float,
    radius_m: float,
    label: str | None,
) -> PrivacyZone:
    """撮影者にプライバシーゾーンを足して保存する。範囲外の値なら ValidationError。"""
    zone = PrivacyZone(
        zone_id=str(uuid.uuid4()),
        lat=lat,
        lon=lon,
        radius_m=radius_m,
        label=label,
        created_at=datetime.now(UTC),
    )
    repository.update_user(user.model_copy(update={"privacy_zones": [*user.privacy_zones, zone]}))
    return zone


def delete_privacy_zone(repository: ObservationRepository, user: User, zone_id: str) -> bool:
    """プライバシーゾーンを消す。見つからなければ False。"""
    zones = [zone for zone in user.privacy_zones if zone.zone_id != zone_id]
    if len(zones) == len(user.privacy_zones):
        return False
    repository.update_user(user.model_copy(update={"privacy_zones": zones}))
    return True


def delete_observation_data(
    observation_id: str,
    record: dict,
    repository: ObservationRepository,
    blob_store: BlobStore,
    weather_store: BlobStore,
    job_repository: JobRepository,
) -> None:
    """観測1件と、それに付くジョブ・天気の生レスポンス・画像を消す。観測そのものは最後に消す。"""
    blob_keys = {
        raw_key(provider, observation_id, phase) for provider in RAW_PROVIDERS for phase in PHASES
    }
    job_ids = [f"{observation_id}_{phase}" for phase in PHASES]
    for job_id in job_ids:
        job = job_repository.get_job(job_id)
        if job is not None:
            blob_keys.update(
                state.blob_key for state in job.providers.values() if state.blob_key is not None
            )
    # 生レスポンスを先に消す。ジョブを先に消すと、途中で失敗したときに blob_key がわからなくなる
    for key in sorted(blob_keys):
        weather_store.delete(key)
    for job_id in job_ids:
        job_repository.delete_job(job_id)
    if record.get("image_key"):
        blob_store.delete(record["image_key"])
    repository.delete_observation(observation_id)


def delete_user_data(
    user_id: str,
    repository: ObservationRepository,
    blob_store: BlobStore,
    weather_store: BlobStore,
    job_repository: JobRepository,
) -> int:
    """撮影者と、その撮影者のデータをすべて消し、消した観測の数を返す。

    観測ごとのデータ、送信数の記録、撮影者の順に消す。途中で失敗しても、もう一度実行すれば
    残りを消せるように、撮影者そのものは最後に消す。
    """
    records = repository.list_observations_by_user(user_id)
    for record in records:
        delete_observation_data(
            record["observation_id"],
            record,
            repository,
            blob_store,
            weather_store,
            job_repository,
        )
    repository.delete_daily_counts(user_id)
    repository.delete_user(user_id)
    return len(records)


def run_due_jobs(
    scheduler: LocalTaskScheduler,
    runner: JobRunner,
    now: datetime,
    clock: Clock = utc_now,
) -> int:
    """期限の来た予約を古い順に1回ずつ実行し、終了コードを返す。

    `now` のときに期限の来ていたものだけを実行する。実行中に新しく入った予約は次の起動で実行する。
    各ジョブの実行に渡す時刻は、1件ごとに `clock` から取り直す。
    """
    failed = False
    for job_id, run_at in scheduler.due(now):
        try:
            result, reason = runner.run(job_id, clock())
        except Exception as e:
            # 予約は消さない。次の起動でまた実行される
            print(f"{job_id} error {type(e).__name__}: {e}")
            failed = True
            continue
        print(f"{job_id} {result} {reason or '-'}")
        # run の中で予約し直されていたら、run_at が違うので消えない
        scheduler.remove(job_id, run_at)
    return 1 if failed else 0


def _latitude(text: str) -> float:
    return _in_range(text, "lat", -90, 90)


def _longitude(text: str) -> float:
    return _in_range(text, "lon", -180, 180)


def _radius(text: str) -> float:
    value = _in_range(text, "radius-m", 0, 50000)
    if value <= 0:
        raise argparse.ArgumentTypeError("radius-m は 0 より大きい値にしてください")
    return value


def _in_range(text: str, name: str, low: float, high: float) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{name} は数値で指定してください: {text!r}") from None
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(
            f"{name} は {low:g} から {high:g} の範囲で指定してください"
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sky_server.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="撮影者を作り、招待コードを1回だけ表示する")
    create.add_argument("--name", required=True)
    revoke = sub.add_parser("revoke-user", help="撮影者の招待コードを無効にする")
    revoke.add_argument("--user-id", required=True)
    add_zone = sub.add_parser("add-privacy-zone", help="撮影者にプライバシーゾーンを追加する")
    add_zone.add_argument("--user-id", required=True)
    add_zone.add_argument("--lat", required=True, type=_latitude)
    add_zone.add_argument("--lon", required=True, type=_longitude)
    add_zone.add_argument("--radius-m", required=True, type=_radius)
    add_zone.add_argument("--label")
    list_zones = sub.add_parser("list-privacy-zones", help="撮影者のプライバシーゾーンを表示する")
    list_zones.add_argument("--user-id", required=True)
    delete_zone = sub.add_parser("delete-privacy-zone", help="プライバシーゾーンを消す")
    delete_zone.add_argument("--user-id", required=True)
    delete_zone.add_argument("--zone-id", required=True)
    consent = sub.add_parser("set-consent", help="データを公開してよいかの同意を変える")
    consent.add_argument("--user-id", required=True)
    consent.add_argument("--public", required=True, choices=["yes", "no"])
    delete = sub.add_parser("delete-user", help="撮影者と、その撮影者のデータをすべて消す")
    delete.add_argument("--user-id", required=True)
    delete.add_argument("--yes", action="store_true", help="確認なしで消す")
    sub.add_parser("run-due-jobs", help="期限の来た天気ジョブを実行する")
    args = parser.parse_args(argv)

    try:
        if args.command == "run-due-jobs" and get_backend_name() == "gcp":
            print(
                "run-due-jobs は SKY_BACKEND=gcp では使えません（Cloud Tasks が実行します）",
                file=sys.stderr,
            )
            return 1
        backend = build_backend(get_data_dir())
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1
    repository = backend.repository
    if args.command == "run-due-jobs":
        scheduler = backend.scheduler
        fetchers = default_fetchers(backend.weather_store)
        runner = JobRunner(repository, backend.job_repository, scheduler, fetchers)
        return run_due_jobs(scheduler, runner, datetime.now(UTC))
    if args.command == "create-user":
        user, token = create_user(repository, args.name)
        print(f"user_id: {user.user_id}")
        print(f"招待コード（この1回だけ表示されます）: {token}")
        return 0

    if args.command == "revoke-user":
        user = revoke_user(repository, args.user_id)
    else:
        user = find_user(repository, args.user_id)
    if user is None:
        print(f"撮影者が見つかりません: {args.user_id}", file=sys.stderr)
        return 1
    if args.command == "revoke-user":
        print(f"無効にしました: {user.user_id}")
    elif args.command == "add-privacy-zone":
        zone = add_privacy_zone(repository, user, args.lat, args.lon, args.radius_m, args.label)
        print(f"zone_id: {zone.zone_id}")
    elif args.command == "list-privacy-zones":
        for zone in user.privacy_zones:
            print(f"{zone.zone_id} {zone.lat} {zone.lon} {zone.radius_m} {zone.label or '-'}")
    elif args.command == "delete-privacy-zone":
        if not delete_privacy_zone(repository, user, args.zone_id):
            print(f"プライバシーゾーンが見つかりません: {args.zone_id}", file=sys.stderr)
            return 1
        print(f"消しました: {args.zone_id}")
    elif args.command == "set-consent":
        public = args.public == "yes"
        repository.update_user(user.model_copy(update={"consent_public": public}))
        print(f"consent_public: {'true' if public else 'false'}")
    else:
        return _delete_user(backend, user, args.yes)
    return 0


def _delete_user(backend: Backend, user: User, yes: bool) -> int:
    count = len(backend.repository.list_observations_by_user(user.user_id))
    if not yes:
        print(
            f"撮影者 {user.user_id} と観測 {count} 件のデータをすべて消します。"
            "消すときは --yes を付けて実行してください",
            file=sys.stderr,
        )
        return 1
    try:
        deleted = delete_user_data(
            user.user_id,
            backend.repository,
            backend.blob_store,
            backend.weather_store,
            backend.job_repository,
        )
    except Exception as e:
        print(
            f"削除の途中で失敗しました: {type(e).__name__}: {e}（もう一度実行すると続きを消せる）",
            file=sys.stderr,
        )
        return 1
    print(f"撮影者 {user.user_id} を消しました（消した観測: {deleted} 件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
