"""管理用コマンド：撮影者の作成と無効化、期限の来た天気ジョブの実行。"""

import argparse
import sys
import uuid
from datetime import UTC, datetime

from sky_server.auth import generate_token, hash_token
from sky_server.backends import build_backend
from sky_server.config import ConfigError, get_backend_name, get_data_dir
from sky_server.jobs import JobRunner, LocalTaskScheduler, default_fetchers
from sky_server.models import User
from sky_server.storage import ObservationRepository
from sky_server.weather.common import Clock, utc_now


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


def revoke_user(repository: ObservationRepository, user_id: str) -> User | None:
    try:
        uuid.UUID(user_id)
    except ValueError:
        return None
    user = repository.get_user(user_id)
    if user is None:
        return None
    if user.revoked_at is None:
        user = user.model_copy(update={"revoked_at": datetime.now(UTC)})
        repository.update_user(user)
    return user


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sky_server.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="撮影者を作り、招待コードを1回だけ表示する")
    create.add_argument("--name", required=True)
    revoke = sub.add_parser("revoke-user", help="撮影者の招待コードを無効にする")
    revoke.add_argument("--user-id", required=True)
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

    user = revoke_user(repository, args.user_id)
    if user is None:
        print(f"撮影者が見つかりません: {args.user_id}", file=sys.stderr)
        return 1
    print(f"無効にしました: {user.user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
