import hashlib

from sky_server.admin import create_user, main, revoke_user
from sky_server.storage import LocalObservationRepository


def test_撮影者を作るとハッシュだけが保存される(tmp_path):
    repo = LocalObservationRepository(tmp_path)
    user, token = create_user(repo, "alice")
    assert user.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert user.revoked_at is None
    assert user.consent_public is False
    saved = (tmp_path / "users" / f"{user.user_id}.json").read_text()
    assert token not in saved


def test_無効にするとrevoked_atが入る(tmp_path):
    repo = LocalObservationRepository(tmp_path)
    user, _ = create_user(repo, "alice")
    revoked = revoke_user(repo, user.user_id)
    assert revoked.revoked_at is not None
    assert repo.get_user(user.user_id).revoked_at is not None


def test_存在しない撮影者の無効化は失敗する(tmp_path):
    assert revoke_user(LocalObservationRepository(tmp_path), "nope") is None


def test_コマンドで作成と無効化ができる(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SKY_DATA_DIR", str(tmp_path))
    assert main(["create-user", "--name", "bob"]) == 0
    out = capsys.readouterr().out
    user_id = out.splitlines()[0].split(": ")[1]
    assert main(["revoke-user", "--user-id", user_id]) == 0
    assert main(["revoke-user", "--user-id", "nope"]) == 1
