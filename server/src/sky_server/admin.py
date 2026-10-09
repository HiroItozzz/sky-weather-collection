"""管理用コマンド：撮影者の作成・無効化・削除、プライバシーゾーンと公開への同意、天気ジョブの実行、要約の作り直し。"""

import argparse
import math
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sky_server.auth import generate_token, hash_token
from sky_server.backends import Backend, build_backend
from sky_server.config import ConfigError, get_backend_name, get_data_dir
from sky_server.jobs import (
    PHASES,
    JobRepository,
    JobRunner,
    LocalTaskScheduler,
    Summarizer,
    _parse_time,
    default_fetchers,
    save_summary,
)
from sky_server.models import PrivacyZone, User, to_utc_millis
from sky_server.storage import BlobStore, ObservationRepository
from sky_server.weather.common import Clock, utc_now

# 天気の生レスポンスの保存先を探すときの提供元（ジョブが残っていなくても消せるようにする）
RAW_PROVIDERS = ("open_meteo", "amedas")

# delete-user の1回目から、撮影者を消せるようになるまでの時間（実行中のジョブの書き戻しを待つ）
DELETION_WAIT = timedelta(minutes=10)


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
    """観測1件と、それに付くジョブ・天気の生レスポンス・画像を消す。観測そのものは最後に消す。

    画像と生レスポンスは、`{id}/` のような接頭辞で消す。同じ ID で別の画像が送られて 409 に
    なったときに、先に保存された画像が残らないようにするため。
    """
    job_ids = [f"{observation_id}_{phase}" for phase in PHASES]
    # ジョブの blob_key が標準の場所と違うときのために、先に集めておく
    blob_keys: set[str] = set()
    for job_id in job_ids:
        job = job_repository.get_job(job_id)
        if job is not None:
            blob_keys.update(
                state.blob_key for state in job.providers.values() if state.blob_key is not None
            )
    # 生レスポンスを先に消す。ジョブを先に消すと、途中で失敗したときに blob_key がわからなくなる
    for provider in RAW_PROVIDERS:
        weather_store.delete_prefix(f"raw/{provider}/{observation_id}/")
    for key in sorted(blob_keys):
        weather_store.delete(key)
    for job_id in job_ids:
        job_repository.delete_job(job_id)
    blob_store.delete_prefix(f"{observation_id}/")
    if record.get("image_key"):
        blob_store.delete(record["image_key"])
    repository.delete_observation(observation_id)


@dataclass(frozen=True)
class DeletionResult:
    """`delete_user_data` の結果。"""

    observation_count: int
    # 撮影者まで消したか。False なら、あと `remaining` たってからもう一度実行する
    finished: bool
    remaining: timedelta


def delete_user_data(
    user_id: str,
    repository: ObservationRepository,
    blob_store: BlobStore,
    weather_store: BlobStore,
    job_repository: JobRepository,
    clock: Clock = utc_now,
) -> DeletionResult:
    """撮影者のデータを2段階で消す。

    実行中のジョブが、消したあとに状態や生レスポンスを書き戻すことがある。そのため、撮影者を
    無効にして消した観測の ID を記録し、観測ごとのデータを消す。撮影者は残し、`DELETION_WAIT`
    以上たってからの実行で、もう一度観測ごとのデータを消したうえで、送信数の記録と撮影者を消す。
    途中で失敗しても、もう一度実行すれば残りを消せる（撮影者そのものは最後に消す）。
    撮影者がいなければ KeyError。
    """
    user = repository.get_user(user_id)
    if user is None:
        raise KeyError(user_id)
    now = clock()
    started_at = user.deletion_started_at or now
    user = user.model_copy(
        update={"revoked_at": user.revoked_at or now, "deletion_started_at": started_at}
    )
    repository.update_user(user)

    ids = list(user.deletion_observation_ids)
    for record in repository.list_observations_by_user(user_id):
        if record["observation_id"] not in ids:
            ids.append(record["observation_id"])
    user = user.model_copy(update={"deletion_observation_ids": ids})
    repository.update_user(user)

    for observation_id in ids:
        record = repository.get_observation(observation_id) or {}
        delete_observation_data(
            observation_id, record, repository, blob_store, weather_store, job_repository
        )

    remaining = started_at + DELETION_WAIT - now
    if remaining > timedelta(0):
        return DeletionResult(len(ids), False, remaining)
    repository.delete_daily_counts(user_id)
    repository.delete_user(user_id)
    return DeletionResult(len(ids), True, timedelta(0))


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


def rebuild_summaries(
    repository: ObservationRepository,
    job_repository: JobRepository,
    summarizer: Summarizer,
) -> tuple[int, int]:
    """すべての観測の `captured_at_utc` と `label_done` を補い、完了したジョブの要約を計算し直す。

    1件の失敗で止めず、`(処理した観測の数, 失敗した観測の数)` を返す。
    要約の計算や観測への書き込みに失敗したジョブには、何も書かない（前の要約を残す）。
    先に観測の ID をすべて読んでから1件ずつ処理するので、処理の途中で観測が増えても減っても
    止まらない（増えた分はこの回では処理せず、消えた分は数えない）。
    """
    processed = failed = 0
    for observation_id in repository.list_all_observation_ids():
        record = repository.get_observation(observation_id)
        if record is None:
            continue
        processed += 1
        try:
            if "captured_at_utc" not in record:
                captured_at_utc = to_utc_millis(_parse_time(record["captured_at"]))
                repository.update_observation_fields(
                    observation_id, {"captured_at_utc": captured_at_utc}
                )
            ok = True
            label_finished = False
            for phase in PHASES:
                job = job_repository.get_job(f"{observation_id}_{phase}")
                if job is None:
                    continue
                if phase == "label" and job.status in ("done", "failed", "skipped"):
                    label_finished = True
                if job.status not in ("done", "failed"):
                    continue
                # save_summary は失敗しても例外を投げないので、成功したかを summary で見分ける。
                # 失敗したときは、ジョブを保存しない（観測にも書かれていない）
                save_summary(job, record, summarizer, repository)
                if job.summary is None:
                    ok = False
                    continue
                job_repository.update_job(job)
            if label_finished and not record.get("label_done"):
                repository.update_observation_fields(observation_id, {"label_done": True})
        except Exception as e:
            print(f"{observation_id} error {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
        else:
            if not ok:
                print(f"{observation_id} error 要約を作れませんでした", file=sys.stderr)
                failed += 1
    return processed, failed


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


def main(argv: list[str] | None = None, clock: Clock = utc_now) -> int:
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
    sub.add_parser("rebuild-summaries", help="天気の要約と撮影時刻（UTC）を作り直す")
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
        summarizer = Summarizer(backend.weather_store)
        runner = JobRunner(
            repository, backend.job_repository, scheduler, fetchers, summarizer=summarizer
        )
        return run_due_jobs(scheduler, runner, datetime.now(UTC))
    if args.command == "rebuild-summaries":
        processed, failed = rebuild_summaries(
            repository, backend.job_repository, Summarizer(backend.weather_store)
        )
        print(f"処理した観測: {processed} 件、失敗: {failed} 件")
        return 1 if failed else 0
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
            print(f"{zone.zone_id}\t{zone.lat}\t{zone.lon}\t{zone.radius_m}\t{zone.label or '-'}")
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
        return _delete_user(backend, user, args.yes, clock)
    return 0


def _delete_user(backend: Backend, user: User, yes: bool, clock: Clock) -> int:
    count = len(backend.repository.list_observations_by_user(user.user_id))
    if not yes:
        print(
            f"撮影者 {user.user_id} と観測 {count} 件のデータをすべて消します。"
            "消すときは --yes を付けて実行してください",
            file=sys.stderr,
        )
        return 1
    try:
        result = delete_user_data(
            user.user_id,
            backend.repository,
            backend.blob_store,
            backend.weather_store,
            backend.job_repository,
            clock,
        )
    except Exception as e:
        print(
            f"削除の途中で失敗しました: {type(e).__name__}: {e}（もう一度実行すると続きを消せる）",
            file=sys.stderr,
        )
        return 1
    if not result.finished:
        minutes = math.ceil(result.remaining.total_seconds() / 60)
        print(
            f"撮影者 {user.user_id} を無効にして、観測 {result.observation_count} 件のデータを"
            f"消しました。撮影者はまだ残っています。あと {minutes} 分以上おいて、"
            "もう一度実行してください"
        )
        return 0
    print(f"撮影者 {user.user_id} を消しました（消した観測: {result.observation_count} 件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
