import asyncio
import json

from conftest import IMAGE, OBSERVATION_ID, make_metadata
from fastapi.testclient import TestClient

from sky_server.admin import create_user
from sky_server.main import create_app
from sky_server.storage import LocalObservationRepository
from sky_server.upload_guard import MAX_INTERNAL_BYTES, MAX_UPLOAD_BYTES, UploadGuard

PATH = f"/v1/observations/{OBSERVATION_ID}"
INTERNAL_PATH = "/internal/tasks/fetch-weather"


class Recorder:
    """ASGI の呼び出しを 1 回行い、応答と受信関数が呼ばれた回数を残す。"""

    def __init__(self, app, headers: list[tuple[bytes, bytes]], chunks: list[bytes], **scope):
        self.app = app
        self.chunks = list(chunks)
        self.receive_calls = 0
        self.messages: list[dict] = []
        self.scope = {
            "type": "http",
            "method": "PUT",
            "path": PATH,
            "headers": headers,
            **scope,
        }

    async def _receive(self) -> dict:
        self.receive_calls += 1
        if not self.chunks:
            return {"type": "http.disconnect"}
        body = self.chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(self.chunks)}

    async def _send(self, message: dict) -> None:
        self.messages.append(message)

    def run(self) -> "Recorder":
        asyncio.run(self.app(self.scope, self._receive, self._send))
        return self

    @property
    def status(self) -> int:
        return self.messages[0]["status"]

    @property
    def headers(self) -> dict[str, str]:
        return {k.decode(): v.decode() for k, v in self.messages[0]["headers"]}

    @property
    def detail(self) -> str:
        return json.loads(self.messages[-1]["body"])["detail"]


class ReadAllApp:
    """本文を最後まで読んで 200 を返すだけのアプリ。ミドルウェアだけを確かめるのに使う。"""

    def __init__(self) -> None:
        self.called = False
        self.state: dict | None = None
        self.read = 0

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.state = scope.get("state")
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            self.read += len(message["body"])
            if not message["more_body"]:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def guarded(api):
    inner = ReadAllApp()
    return inner, UploadGuard(inner, api.repository)


def auth(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())]


def length(n) -> tuple[bytes, bytes]:
    return (b"content-length", str(n).encode())


def test_Content_Lengthが数字でなければ400(api, token):
    inner, guard = guarded(api)
    for value in (b"abc", b"-1", b"1.5", b""):
        r = Recorder(guard, auth(token) + [(b"content-length", value)], [b"x"]).run()
        assert r.status == 400
    assert not inner.called


def test_Content_Lengthが上限を超えていれば本文を読まずに413(api, token):
    inner, guard = guarded(api)
    r = Recorder(guard, auth(token) + [length(MAX_UPLOAD_BYTES + 1)], [b"x"]).run()
    assert r.status == 413
    assert r.receive_calls == 0
    assert not inner.called


def test_大きさの検査は認証より先に行う(api):
    _, guard = guarded(api)
    r = Recorder(guard, [length(MAX_UPLOAD_BYTES + 1)], []).run()
    assert r.status == 413


def test_未認証は本文を読む前に401(api):
    inner, guard = guarded(api)
    for headers in ([], auth("wrong-token"), [(b"authorization", b"Basic abc")]):
        r = Recorder(guard, headers + [length(10)], [b"x" * 10]).run()
        assert r.status == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.headers["content-type"] == "application/json"
        assert r.detail == "認証に失敗しました"
        assert r.receive_calls == 0
    assert not inner.called


def test_無効にされた招待コードは本文を読む前に401(api):
    from sky_server.admin import revoke_user

    user_id, token = api.new_user("revoked")
    revoke_user(api.repository, user_id)
    inner, guard = guarded(api)
    r = Recorder(guard, auth(token) + [length(10)], [b"x" * 10]).run()
    assert r.status == 401
    assert r.receive_calls == 0


def test_認証できたら撮影者をstateに入れて通す(api, token):
    inner, guard = guarded(api)
    r = Recorder(guard, auth(token) + [length(5)], [b"hello"]).run()
    assert r.status == 200
    assert inner.read == 5
    assert inner.state["user"].name == "tester"


def test_Content_Lengthがなくても認証すれば受け付ける(api, token):
    inner, guard = guarded(api)
    r = Recorder(guard, auth(token), [b"abc", b"def"]).run()
    assert r.status == 200
    assert inner.read == 6


def test_Content_Lengthがなくても未認証なら本文を読む前に401(api):
    inner, guard = guarded(api)
    r = Recorder(guard, [], [b"abc"]).run()
    assert r.status == 401
    assert r.receive_calls == 0


def test_本文がちょうど上限のバイト数なら通る(api, token):
    inner, guard = guarded(api)
    chunks = [b"x" * 1024 * 1024] * 11
    r = Recorder(guard, auth(token), chunks).run()
    assert r.status == 200
    assert inner.read == MAX_UPLOAD_BYTES


def test_本文が上限より1バイト多ければ413(api, token):
    inner, guard = guarded(api)
    chunks = [b"x" * 1024 * 1024] * 11 + [b"x"]
    r = Recorder(guard, auth(token), chunks).run()
    assert r.status == 413
    assert r.detail == "リクエストが大きすぎます"
    assert inner.read == MAX_UPLOAD_BYTES


def test_Content_Lengthを小さく申告して上限を超えて送れば413(api, token):
    inner, guard = guarded(api)
    chunks = [b"x" * 1024 * 1024] * 12
    r = Recorder(guard, auth(token) + [length(100)], chunks).run()
    assert r.status == 413
    # 超えた時点で読むのをやめる
    assert r.receive_calls == 12
    assert inner.read <= MAX_UPLOAD_BYTES


def test_本物のアプリでもContent_Lengthを偽った大きな本文は413(api, token):
    app = api.client.app
    head = b'--b\r\nContent-Disposition: form-data; name="image"; filename="a.jpg"\r\n\r\n'
    chunks = [head] + [b"x" * 1024 * 1024] * 12
    headers = auth(token) + [length(100), (b"content-type", b"multipart/form-data; boundary=b")]
    r = Recorder(app, headers, chunks, query_string=b"").run()
    assert r.status == 413
    assert r.detail == "リクエストが大きすぎます"
    assert len([m for m in r.messages if m["type"] == "http.response.start"]) == 1


def test_PUT以外とアップロード以外のパスには効かない(api):
    inner, guard = guarded(api)
    # 認証がなくても、検査されずにそのまま通る
    r = Recorder(guard, [length(MAX_UPLOAD_BYTES + 1)], [b"x"], method="GET").run()
    assert r.status == 200
    r = Recorder(guard, [], [b"x"], path="/v1/other").run()
    assert r.status == 200
    r = Recorder(guard, [], [b"x"], path=INTERNAL_PATH, method="GET").run()
    assert r.status == 200
    r = Recorder(guard, [], [b"x"], path=f"{PATH}/extra").run()
    assert r.status == 200


def test_GETは認証がなければアプリの401になる(api):
    res = api.client.get(PATH)
    assert res.status_code == 401
    assert res.json() == {"detail": "認証に失敗しました"}


def test_正常なアップロードは通り撮影者の照合は1回だけ(data_dir):
    class CountingRepository(LocalObservationRepository):
        lookups = 0

        def get_user_by_token_hash(self, token_hash):
            self.lookups += 1
            return super().get_user_by_token_hash(token_hash)

    repository = CountingRepository(data_dir)
    _, token = create_user(repository, "tester")
    client = TestClient(create_app(data_dir, repository=repository))
    res = client.put(
        PATH,
        headers={"Authorization": f"Bearer {token}"},
        data={"metadata": json.dumps(make_metadata())},
        files={"image": ("photo.jpg", IMAGE, "image/jpeg")},
    )
    assert res.status_code == 201
    assert repository.lookups == 1


def test_Content_Lengthのない分割送信でも認証済みなら通る(api, token):
    def body():
        yield b"--b\r\n"
        yield b"x"

    res = api.client.put(PATH, headers={"Authorization": f"Bearer {token}"}, content=body())
    # multipart として不正なので 4xx だが、ミドルウェアが断った 401/413 ではない
    assert res.status_code not in (401, 411, 413)


def internal(app, headers, chunks) -> Recorder:
    return Recorder(app, headers, chunks, path=INTERNAL_PATH, method="POST").run()


def test_内部APIは認証なしで4KBちょうどの本文が通る(api):
    inner, guard = guarded(api)
    assert MAX_INTERNAL_BYTES == 4 * 1024
    r = internal(guard, [length(MAX_INTERNAL_BYTES)], [b"x" * MAX_INTERNAL_BYTES])
    assert r.status == 200
    assert inner.read == MAX_INTERNAL_BYTES
    # 認証はミドルウェアでは行わないので、撮影者は入らない
    assert "user" not in (inner.state or {})


def test_内部APIはContent_Lengthが4KBを超えていれば本文を読まずに413(api):
    inner, guard = guarded(api)
    r = internal(guard, [length(MAX_INTERNAL_BYTES + 1)], [b"x"])
    assert r.status == 413
    assert r.detail == "リクエストが大きすぎます"
    assert r.receive_calls == 0
    assert not inner.called


def test_内部APIは本文が4KBより1バイト多ければ413(api):
    inner, guard = guarded(api)
    r = internal(guard, [], [b"x" * MAX_INTERNAL_BYTES, b"x"])
    assert r.status == 413
    assert inner.read == MAX_INTERNAL_BYTES


def test_内部APIはContent_Lengthを小さく偽って大きく送ると413(api):
    inner, guard = guarded(api)
    chunks = [b"x" * 1024] * 8
    r = internal(guard, [length(10)], chunks)
    assert r.status == 413
    # 超えた時点で読むのをやめる
    assert r.receive_calls == 5
    assert inner.read <= MAX_INTERNAL_BYTES


def test_内部APIはContent_Lengthが数字でなければ400(api):
    inner, guard = guarded(api)
    for value in (b"abc", b"-1", b"1.5", b""):
        r = internal(guard, [(b"content-length", value)], [b"x"])
        assert r.status == 400
    assert not inner.called


def test_内部APIの大きな本文はOIDCの検証より前に断られる(data_dir, monkeypatch):
    calls = []

    def verify(request):
        calls.append(request)

    app = create_app(data_dir, task_auth=verify)
    body = b'{"job_id": "' + b"x" * MAX_INTERNAL_BYTES + b'"}'
    # Content-Length が正しい場合
    r = internal(app, [length(len(body))], [body])
    assert r.status == 413
    # Content-Length を偽った場合
    r = internal(app, [length(10)], [body[i : i + 1024] for i in range(0, len(body), 1024)])
    assert r.status == 413
    assert calls == []


def test_内部APIの小さな本文は検証に進む(data_dir):
    calls = []
    app = create_app(data_dir, task_auth=calls.append)
    body = json.dumps({"job_id": f"{OBSERVATION_ID}_forecast"}).encode()
    headers = [length(len(body)), (b"content-type", b"application/json")]
    r = Recorder(app, headers, [body], path=INTERNAL_PATH, method="POST", query_string=b"").run()
    assert r.status == 200
    assert len(calls) == 1
