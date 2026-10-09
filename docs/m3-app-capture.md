# M3 の仕様：撮影、端末内への保存、再送キュー、アップロード

M3 では、空の写真を撮って、設計メモ4節のメタデータと一緒に端末内に保存し、M1 のサーバー（12節の API）へ送る。送れないときは端末内に溜めておき、あとで自動で送り直す。

この文書は、調べた結果（2026-10、Expo SDK 57 のソースを読んで確認）と、それにもとづく判断、実装の仕様をまとめたもの。M2 の向きの計算（`docs/m2-app-sensors.md`）はそのまま使う。

## 1. 調べた結果

### 1.1 撮影（expo-camera の `takePictureAsync`）

`expo-camera` 57.0.6 の `ResolveTakenPicture.kt` を読んで確認した。

- 既定（`skipProcessing: false`）では、カメラが出した JPEG をいったんビットマップに戻し、EXIF の向きに合わせて回転してから、`quality` で JPEG に圧縮し直す。`exif: true` なら、そのあと元の EXIF のタグを書き戻す。つまり画質が一度落ち、画素の向きと EXIF の `Orientation` が食い違うおそれもある。
- `skipProcessing: true` では、カメラ（CameraX）が出した JPEG をそのままキャッシュのディレクトリに書き出す。EXIF も元のまま。画素は撮像素子の向きのままで、表示の向きは EXIF の `Orientation` で表される。幅と高さは EXIF の `ImageWidth` / `ImageLength` から読む（ないときは -1）。
- `exif: true` のとき JS に返る EXIF は、`ExifInterface` のタグ名をキーにした値。型はタグごとに決まっている：`ExposureTime`・`FNumber`・`FocalLength` は数値（double）、`ISOSpeedRatings`・`FocalLengthIn35mmFilm`・`WhiteBalance`・`ImageWidth`・`ImageLength`・`PixelXDimension`・`PixelYDimension`・`Orientation` は整数、`DateTimeOriginal` などは文字列。カメラが書かなかったタグはキーごとない。
- どのタグが入るかは機種しだい（CameraX は撮影結果から露出時間・ISO・焦点距離・ホワイトバランスなどを書くが、確かめていない）。

### 1.2 ファイル（expo-file-system）

- SDK 57 の新しい API（`File`、`Directory`、`Paths`）を使う。`Paths.document` はアプリ専用の保存先で、アプリを消すまで残る。キャッシュ（`Paths.cache`）は OS に消されることがあるので、撮った写真はすぐに `Paths.document` の下へ移す。
- `File` には `bytes()`（Promise で `Uint8Array`）、`text()`、`write()`（同期）、`move()` / `copy()`（Promise）、`delete()`、`exists`、`size` がある。SHA-256 を直接求める機能はない（`md5` だけ）。

### 1.3 そのほかのモジュール（すべて Expo Go に含まれる）

- `expo-crypto`：`digest(SHA256, bytes)` でハッシュ、`randomUUID()` で UUID。
- `expo-secure-store`：Android Keystore で暗号化して保存する。招待コードの保存に使う。
- `expo-network`：`addNetworkStateListener` で、電波が戻ったことを知る。
- `expo-device`：`modelName`、`osVersion`。
- `expo-constants`：`expoConfig.version` で `app.json` のバージョン（Expo Go では、`expo-application` の値は Expo Go 自体のバージョンになるので使わない）。

### 1.4 アップロード

- React Native の `FormData` は、ファイルを `{ uri, name, type }` の形で受け取り、ファイルの中身を multipart で送る。`fetch` は PUT でも使える。
- サーバー（`server/src/sky_server/models.py`）は、`location.accuracy_m` を null 不可、`device` の各値を null 不可の文字列にしている。`camera` はオブジェクト自体は必須で、各値は null 可。

### 1.5 平文の HTTP

- Expo Go は、開発用のサーバー（`http://` の LAN の IP）につなぐために平文の HTTP を許している。M3 でローカルのサーバー（`http://<PC の IP>:8000`）に送れるのはこのため。
- 自前でビルドしたアプリでは、Android は既定で平文の HTTP を禁止する。本番（Cloud Run、HTTPS）では問題にならない。

## 2. 判断

| 項目 | 判断 | 理由 |
|---|---|---|
| 撮影の方法 | `takePictureAsync({ skipProcessing: true, exif: true })` | 設計メモ12節「写真は元の画像のまま（EXIF 付き）保存」。圧縮し直しで画質が落ちず、EXIF も元のまま |
| `image_width` / `image_height` | 保存した JPEG の画素の幅と高さ（EXIF の向きで回す前）。EXIF の `ImageWidth` / `ImageLength`、なければ `PixelXDimension` / `PixelYDimension`、なければ `takePictureAsync` の戻り値。0 以下は null | サーバーに送る画像そのものの寸法にそろえる |
| `captured_at` | シャッターのボタンを押した時刻（`Date.now()`） | 向きのサンプルと同じ時計で比べられる。EXIF の時刻はタイムゾーンがない |
| 向き | M2 の `buildRecord` と同じく、押した時刻にいちばん近いサンプルと、前後 0.5 秒のばらつき | M2 と同じ |
| 位置の精度が取れないとき | `location` 全体を null にする（`coords.accuracy` が null か 0 以下のとき。Android は精度がないと 0 を返す） | サーバーが `accuracy_m` を null 不可にしている。精度のわからない位置はラベルの取得に使いにくい |
| 古い位置 | そのまま送る（`fix_time` で判断できる）。画面では、測ってから 60 秒以上たっていたら警告を出す | 捨てるかどうかは後で決められる |
| シャッターの制限 | 仰角が 20° 以上のときだけ押せる。向きがまだ取れていないときも押せない | 設計メモ6節 |
| キャリブレーションの促し | 方位の精度が `low` か `unreliable`、または地磁気の実測が WMM の全磁力から 20% 以上ずれているとき、8の字に振るよう表示する。シャッターは止めない | 設計メモ6節。止めると撮れなくなる機種が出るかもしれないので、まずは表示だけ |
| 招待コードの保存先 | `expo-secure-store` | 端末内で暗号化される |
| サーバーの URL の保存先 | 同じく `expo-secure-store`（秘密ではないが、保存先を1つにするため） | |
| 送信後の画像 | 送信に成功したら画像を消し、メタデータと状態は残す | 1枚 3〜6MB あるので、残すと容量を圧迫する。メタデータは M5 のタイムラインに使える |
| `user_guess` | 常に null | M5 で扱う |

## 3. 端末内の保存の形

`Paths.document/observations/{observation_id}/` に、1件ずつディレクトリを作る。

| ファイル | 中身 |
|---|---|
| `image.jpg` | 撮った JPEG（送信に成功したら消す） |
| `metadata.json` | サーバーに送るメタデータ（4節の全項目）。一度書いたら変えない |
| `status.json` | 送信の状態（下記）。なければ `pending` とみなす |

`status.json` の形：

```json
{ "state": "pending", "attempts": 2, "last_attempt_at": "...", "next_attempt_at": "...", "last_error": "..." }
```

- `state` は `pending`（送る前・失敗して待っている）、`sent`（送信済み）、`rejected`（サーバーに断られた。自動では送り直さない）のどれか。`sent` のときは `sent_at` も持つ。
- `rejected` のときの `last_error` には、HTTP の状態コードとサーバーの `detail` を短くして入れる。

### 保存の順番（途中でアプリが落ちても壊れないように）

1. `takePictureAsync` の結果（キャッシュの中のファイル）を `observations/{id}/image.jpg` へ移す。
2. 画像を読んで SHA-256 を求める。
3. `metadata.json` を書く。**これを書き終えた時点で保存の完了とする。**
4. 送信を始める（4節）。

- 起動時に `observations/` を見て、`metadata.json` のない（または読めない）ディレクトリは、作ってから 1 時間以上たっていれば消す（保存の途中で落ちたもの。メタデータがないので送れない）。1 時間以内のものは、いま保存している最中かもしれないので触らない。
- `status.json` が読めないときは `pending` とみなす。PUT は冪等（同じ内容なら 200）なので、送信済みのものをもう一度送っても害はない。
- 送信に成功したら、`status.json` を `sent` にしてから `image.jpg` を消す。逆の順番だと、消した後に落ちたとき、画像のない `pending` が残って送れなくなる。

## 4. 送信と再送

### 4.1 1件の送り方

- `PUT {サーバーの URL}/v1/observations/{observation_id}`、ヘッダー `Authorization: Bearer {招待コード}`、本文は multipart（`metadata` は JSON の文字列、`image` は `{ uri, name: "image.jpg", type: "image/jpeg" }`）。
- 60 秒で打ち切る（`AbortController`）。
- 送る前に画像の大きさを見て、10MB を超えていたら送らずに `rejected`（`last_error` に「画像が10MBを超えています」）にする。

### 4.2 応答の分類

| 応答 | 分類 | 状態の変え方 |
|---|---|---|
| 201、200 | 成功 | `sent` |
| 401 | 認証の失敗 | 状態は `pending` のまま。キュー全体を止め、設定の画面で招待コードを確かめるよう表示する。設定を保存し直すまで自動では送らない |
| 409、413、422、その他の 4xx（408・429 を除く） | 断られた | `rejected` |
| 408、429、5xx、通信のエラー、打ち切り | 一時的な失敗 | `pending` のまま、`attempts` を1増やし、`next_attempt_at` を決める |

- 一時的な失敗の待ち時間は `min(30秒 × 2^(attempts-1), 30分)`。
- `rejected` は自動では送り直さない。送信状況の画面で「送り直す」を押すと `pending` に戻す（サーバーを直したあとなどのため）。

### 4.3 いつ送るか

- 次のときは、`next_attempt_at` を待たずに、`pending` のものをすべて送ってみる：アプリの起動、アプリが前面に戻ったとき、電波が戻ったとき（`expo-network` で「つながっていない」から「つながった」に変わったとき）、撮影して保存したあと、送信状況の画面の「今すぐ送る」。
- アプリを開いている間は 30 秒ごとに見て、`next_attempt_at` を過ぎた `pending` を送る。
- 送るのは古い順（`captured_at` の昇順）に1件ずつ。送っている最中にまた呼ばれたら、今の送信が終わったあとにもう1周する（同時に2つは走らせない）。
- 1周の途中で 401 か、通信のエラーが出たら、その周は打ち切る（電波がないのに全件を試さないため）。
- サーバーの URL か招待コードが未設定なら送らない。
- バックグラウンドでの送信（アプリを閉じている間）は M3 ではやらない。

## 5. メタデータの作り方（`app/src/metadata.ts`）

`buildMetadata(input)` を純粋な関数として作る。入力は、`observationId`、押した時刻、前後 0.5 秒の向きのサンプル、最新の位置、heading の accuracy、EXIF と `takePictureAsync` の幅・高さ、端末の情報（platform、os_version、model、app_version）、画像の SHA-256。

- `orientation` と `location` は M2 の `buildRecord` の結果を使う。ただし `location` は、`coords.accuracy` が null か 0 以下なら null にする。
- `tz_offset_min` は `-new Date(押した時刻).getTimezoneOffset()`。
- `camera` は `parseCameraExif(exif, width, height)`（`app/src/exif.ts`）。
  - 数値のタグは、数値ならそのまま、文字列なら `"1/100"` の形の分数か10進数として読む。読めない値、有限でない値は null。
  - `focal_length_mm` ← `FocalLength`、`focal_length_35mm` ← `FocalLengthIn35mmFilm`（0 は「不明」の意味なので null）、`exposure_time_s` ← `ExposureTime`、`iso` ← `ISOSpeedRatings`（なければ `PhotographicSensitivity`。整数に丸める）、`f_number` ← `FNumber`、`white_balance` ← `WhiteBalance`（0 は `"auto"`、1 は `"manual"`、それ以外とないときは null）。
  - 幅と高さは2節の判断のとおり。
  - EXIF そのものが null か undefined なら、幅と高さ以外はすべて null。
- `device.platform` は `"android"` か `"ios"`（それ以外の環境では撮影させない）。`os_version`・`model`・`app_version` が取れないときは `"unknown"`。
- `capture_path` は `"native"`、`user_guess` は null、`schema_version` は 1。
- 項目の名前と並びは4節（サーバーの `ObservationMetadata`）にそろえ、余計な項目を入れない（サーバーは知らない項目を 422 にする）。

テスト：全項目がそろっていること（キーの一覧をサーバーのモデルと照らし合わせる）、精度なしで `location` が null、EXIF なしで `camera` の値が null、分数の文字列、`FocalLengthIn35mmFilm` が 0、`WhiteBalance` の対応、幅と高さの決め方、`tz_offset_min` の符号（日本なら 540）。

## 6. コードの分け方

純粋な部分（jest でテストする）と、Expo に依存する部分を分ける。

| ファイル | 中身 | テスト |
|---|---|---|
| `src/exif.ts` | `parseCameraExif` | あり |
| `src/metadata.ts` | `buildMetadata` | あり |
| `src/uploadQueue.ts` | 送信の段取り（4節）。保存先と送信を、下のインターフェースで受け取る | あり（偽の保存先と送信で） |
| `src/transport.ts` | `fetch` で PUT し、応答を4.2の分類にする。`classifyResponse(status)` は純粋な関数 | `classifyResponse` だけ |
| `src/observationStore.ts` | 3節の保存（expo-file-system） | なし（実機で確かめる） |
| `src/settings.ts` | サーバーの URL と招待コードの読み書き（expo-secure-store）。`normalizeServerUrl` は純粋な関数 | `normalizeServerUrl` だけ |
| `src/useUploadQueue.ts` | 4.3 のきっかけ（起動、前面、電波、タイマー）をつなぐフック | なし |
| `App.tsx`、`src/screens/` | 撮影の画面と、設定・送信状況の画面 | なし |

`uploadQueue.ts` が受け取るインターフェース（形は実装で整えてよい）：

```ts
type QueueItem = { id: string; capturedAt: string; status: Status };
interface Store {
  list(): Promise<QueueItem[]>;                  // metadata.json のそろったものだけ
  readMetadataJson(id: string): Promise<string>;
  image(id: string): Promise<{ part: ImagePart; size: number } | null>; // 画像がなければ null
  setStatus(id: string, status: Status): Promise<void>;
  deleteImage(id: string): Promise<void>;
}
type ImagePart = { uri: string; name: string; type: string } | Blob;  // 端末では前者、テストとノードでは Blob
interface Transport {
  put(baseUrl: string, token: string, id: string, metadataJson: string, image: ImagePart): Promise<Outcome>;
}
type Outcome = { kind: "ok" } | { kind: "auth" } | { kind: "rejected"; error: string } | { kind: "retry"; error: string };
```

- 時刻は `now()` を引数で受け取り、テストで差し替えられるようにする。

`normalizeServerUrl`：前後の空白を取り、末尾の `/` を取る。`http://` か `https://` で始まらなければ null（エラー）。

## 7. 画面

### 7.1 撮影の画面（起動したときの画面）

- 背景いっぱいにカメラのプレビュー、中央に照準（M2 と同じ）。
- 上に小さな板：方位角（真北）、仰角、ロール、方位の精度、位置の精度。M2 の詳しい表示（地磁気の強さなど）は「詳細」を押したときだけ出す。
- 警告の帯（あてはまるものだけ）：キャリブレーションの促し（2節）、位置がない・古い、カメラの許可がない、サーバーが未設定、招待コードが違う（401）。
- 下にシャッターのボタン。仰角が 20° 未満なら押せず、「空に向けてください（仰角 20° 以上）」と出す。
- 押したら、撮影 → 0.5 秒たつまで待つ → 保存（3節）→ 「保存しました」と短く表示 → 送信を始める。保存が終わるまでシャッターは押せない。保存に失敗したら、その旨を表示する。
- 右上に「未送信 N 件」と、設定・送信状況の画面へのボタン。

### 7.2 設定・送信状況の画面

- サーバーの URL と招待コードの入力（招待コードは伏せ字）。「保存」で `normalizeServerUrl` を通して保存する。保存したら 401 で止めていたキューを再開する。
- 「接続を確かめる」：`GET /healthz` が 200 か、`GET /v1/observations/{ランダムな UUID}` が 404（認証は通った）か 401（招待コードが違う）かを表示する。
- 送信状況の一覧：新しい順に、撮影時刻、状態（未送信・送信済み・断られた）、失敗の回数と最後のエラー。断られたものには「送り直す」。上に「今すぐ送る」。

## 8. サーバーとつないで確かめる

### 8.1 自動のテスト（`app/src/e2e.test.ts`）

環境変数 `SKY_E2E_URL` と `SKY_E2E_TOKEN` があるときだけ動く（ないときは skip）。`npm run test:e2e` で流す。jest の `node` 環境で、本物の `transport.ts` と、メモリの上の偽の保存先（画像は `Blob`）を使う。`jest-expo` のプリセットはグローバルの `fetch` を Expo の実装に差し替える（`node` 環境でも差し替わり、応答の `status` が取れない）ので、テストの中に `node:http` で書いた最小の `fetch` を置き、`createFetchTransport` に渡す。`FormData` と `Blob` は Node のものを使う。

1. 小さな JPEG（テストの中にバイト列で持つ）と `buildMetadata` で作ったメタデータを1件入れて送る → `sent`、サーバーの `GET` で `received_at` が返る。
2. 同じものを `pending` に戻してもう一度送る → 200 で `sent`（再送）。
3. 通じない URL（`http://127.0.0.1:9`）で送る → `pending` のまま、`attempts` が 1、`next_attempt_at` が 30 秒後。続けて正しい URL で送る → `sent`（オフラインの後の再送）。
4. 違う招待コードで送る → `pending` のまま、キューが止まる。
5. 同じ ID で画像だけ違うもの → `rejected`（409）。

サーバーの起動のしかた（`server/README.md` も参照）：

```bash
cd server
export SKY_DATA_DIR=$(mktemp -d)
uv run python -m sky_server.admin create-user --name e2e   # 表示された招待コードを控える
uv run uvicorn sky_server.main:create_app --factory --port 8000
# 別のターミナルで
cd app
SKY_E2E_URL=http://127.0.0.1:8000 SKY_E2E_TOKEN=<招待コード> npm run test:e2e
```

### 8.2 実機で確かめる手順（ユーザー向け）

準備：PC でサーバーを `--host 0.0.0.0` を付けて起動し（`uv run uvicorn sky_server.main:create_app --factory --host 0.0.0.0 --port 8000`）、撮影者を作る。端末と PC を同じ Wi-Fi につなぐ。アプリの設定で、サーバーの URL（`http://<PC の IP>:8000`）と招待コードを入れて「接続を確かめる」。

1. **送信**：空に向けて撮る。数秒で「未送信」が 0 になり、サーバーの `SKY_DATA_DIR` に画像とメタデータができる。
2. **メタデータ**：サーバーに保存されたメタデータの `camera` に EXIF の値（焦点距離、露出時間、ISO など）が入っているか。null の項目をメモする。
3. **画像**：サーバーに保存された画像の SHA-256 が `image_sha256` と同じか（サーバーが検査している）。画像を PC で開いて向きが正しいか（EXIF の向きを読むビューアーで）。大きさ（MB）をメモする。
4. **仰角の制限**：地平線に向けるとシャッターが押せず、20° 以上で押せる。
5. **機内モード**：機内モードで3枚撮る。「未送信 3 件」になる。
6. **電波が戻ったとき**：機内モードを切る。アプリを開いたまま、1分以内に3件とも送られる。
7. **アプリを閉じていたとき**：機内モードで撮り、アプリを完全に終了してから機内モードを切り、アプリを開く。起動してすぐ送られる。
8. **サーバーが止まっているとき**：サーバーを止めて撮る。「未送信」のまま、失敗の回数が増える。サーバーを起動し直すと、30 秒〜数分のうちに送られる（待ち時間が延びていたら「今すぐ送る」で即座に送れる）。
9. **招待コードが違うとき**：設定でコードを変えて撮る。「招待コードが違う」と表示され、送られない。正しいコードに戻すと送られる。
10. **キャリブレーション**：PC やスピーカーに近づけると、8の字に振るよう表示される。
