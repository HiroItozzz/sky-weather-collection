"""管理用コマンド：撮影者の作成と無効化。"""

import argparse
import sys
import uuid
from datetime import UTC, datetime

from sky_server.auth import generate_token, hash_token
from sky_server.config import get_data_dir
from sky_server.models import User
from sky_server.storage import LocalObservationRepository


def create_user(repository: LocalObservationRepository, name: str) -> tuple[User, str]:
    token = generate_token()
    user = User(
        user_id=str(uuid.uuid4()),
        name=name,
        token_hash=hash_token(token),
        created_at=datetime.now(UTC),
    )
    repository.add_user(user)
    return user, token


def revoke_user(repository: LocalObservationRepository, user_id: str) -> User | None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sky_server.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="撮影者を作り、招待コードを1回だけ表示する")
    create.add_argument("--name", required=True)
    revoke = sub.add_parser("revoke-user", help="撮影者の招待コードを無効にする")
    revoke.add_argument("--user-id", required=True)
    args = parser.parse_args(argv)

    repository = LocalObservationRepository(get_data_dir())
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
