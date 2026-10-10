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

- Expo（SDK 52 以降）では、グローバルの `fetch` が `expo/fetch` に置き換わっている。その `FormData` はファイルを `{ uri, name, type }` の形では受け取れず（`Unsupported FormDataPart implementation` になる）、`bytes()` を持つもの（`expo-file-system` の `File` など）を読み込んで送る。そこで画像は `{ name, type, bytes: () => file.bytes() }` の形で渡す。`name` と `type` はパートのヘッダー（`filename`、`Content-Type`）に使われる。（実機の確認で判明）
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
| シャッターの制限 | 仰角が 15° 以上のときだけ押せる。向きがまだ取れていないときも押せない | 設計メモ6節 |
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

`metadata.json` と `status.json` は、同じディレクトリの一時ファイル（`*.tmp`）に書いてから `move` で置き換える。書いている途中で落ちても、中身が半分のファイルが残らないようにするため。
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

- 起動時に `observations/` を見て、`metadata.json` が**存在しない**ディレクトリは、作ってから 1 時間以上たっていれば消す（保存の途中で落ちたもの。メタデータがないので送れない）。1 時間以内のものは、いま保存している最中かもしれないので触らない。
- `metadata.json` が存在するのに読めない（0 バイト、壊れている、読み込みの一時的な失敗）ディレクトリは、消さずに残し、送信の対象からも外す。画像を失わないため（後で原因を調べられる）。
- `status.json` が読めないときは `pending` とみなす。PUT は冪等（同じ内容なら 200）なので、送信済みのものをもう一度送っても害はない。
- 送信に成功したら、`status.json` を `sent` にしてから `image.jpg` を消す。逆の順番だと、消した後に落ちたとき、画像のない `pending` が残って送れなくなる。

## 4. 送信と再送

### 4.1 1件の送り方

- `PUT {サーバーの URL}/v1/observations/{observation_id}`、ヘッダー `Authorization: Bearer {招待コード}`、本文は multipart（`metadata` は JSON の文字列、`image` は `{ name: "image.jpg", type: "image/jpeg", bytes }`）。
- 打ち切りの時間は `max(60秒, 画像の大きさ ÷ 50KB/秒)`（`AbortController`）。遅い回線でも大きな画像を送りきれるようにするため（5MB なら約 100 秒）。
- 送る前に画像の大きさを見て、10MB を超えていたら送らずに `rejected`（`last_error` に「画像が10MBを超えています」）にする。

### 4.2 応答の分類

| 応答 | 分類 | 状態の変え方 |
|---|---|---|
| 201、200 | 成功 | `sent` |
| 401 | 認証の失敗 | 状態は `pending` のまま。キュー全体を止め、設定の画面で招待コードを確かめるよう表示する。設定を保存し直すまで自動では送らない |
| 403、404、405 | 設定の誤り | 401 と同じく、状態は `pending` のまま、キュー全体を止める。設定の画面でサーバーの URL を確かめるよう表示する（URL にパスを含めた、プロキシに止められた、など） |
| 422 で、`detail` に `captured_at がサーバー時刻より` を含むもの | 一時的な失敗 | 端末の時計が進んでいるときに起きる。時計が直れば送れるので、下の「一時的な失敗」と同じに扱う |
| 409、413、その他の 422、その他の 4xx（408・429 を除く） | 断られた | `rejected` |
| 408、429、5xx、通信のエラー、打ち切り | 一時的な失敗 | `pending` のまま、`attempts` を1増やし、`next_attempt_at` を決める |

- `detail` の判定は、短くする前の本文で行う。

- 一時的な失敗の待ち時間は `min(30秒 × 2^(attempts-1), 30分)`。
- `rejected` は自動では送り直さない。送信状況の画面で「送り直す」を押すと `pending` に戻す（サーバーを直したあとなどのため）。
- 設定を保存し直したら、止めていたキューを再開し、あわせて `last_error` が `HTTP 403`・`HTTP 404`・`HTTP 405` で始まる `rejected` を `pending` に戻す（これらを `rejected` にしていた古い版のアプリで撮ったもののため）。
- 送っている最中に設定が保存し直されたとき、古い設定で送った PUT の 401・403・404・405 でキューを止め直さない。1周を始めるときに設定の世代（保存のたびに1増える番号）を覚えておき、応答が返った時点で世代が変わっていたら、止めずにその周を終える。

### 4.3 いつ送るか

- 次のときは、`next_attempt_at` を待たずに、`pending` のものをすべて送ってみる：アプリの起動、アプリが前面に戻ったとき、電波が戻ったとき（`expo-network` で「つながっていない」から「つながった」に変わったとき）、撮影して保存したあと、送信状況の画面の「今すぐ送る」。
- アプリを開いている間は 30 秒ごとに見て、`next_attempt_at` を過ぎた `pending` を送る。
- 送るのは古い順（`captured_at` の昇順）に1件ずつ。送っている最中にまた呼ばれたら、今の送信が終わったあとにもう1周する（同時に2つは走らせない）。
- 1周の途中で、キューを止める応答（401・403・404・405）か、通信の状態が悪いことを示す失敗（通信のエラー、打ち切り、408、429、503）が出たら、その周は打ち切る（電波がないのに全件を試さないため）。それ以外の一時的な失敗（500 など）は、その1件だけ待ちに回して次の件へ進む（特定の1件のせいで他が詰まらないように）。
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
type ImagePart = { name: string; type: string; bytes: () => Promise<Uint8Array> } | Blob;  // 端末では前者、テストとノードでは Blob
interface Transport {
  put(baseUrl: string, token: string, id: string, metadataJson: string, image: ImagePart, imageSize: number): Promise<Outcome>;
}
type Outcome =
  | { kind: "ok" }
  | { kind: "blocked"; reason: "auth" | "config"; error: string }  // 401 / 403・404・405
  | { kind: "rejected"; error: string }
  | { kind: "retry"; error: string; stopPass: boolean };          // stopPass: その周を打ち切るか
```

- 時刻は `now()` を引数で受け取り、テストで差し替えられるようにする。

`normalizeServerUrl`：前後の空白を取り、末尾の `/` を取る。`http://` か `https://` で始まらなければ null（エラー）。ホストの後ろにパス（`/v1` など）、`?`、`#` があっても null（エラー。`/v1/v1/...` のような URL になるのを防ぐ）。画面では「`http://192.168.0.10:8000` のように、パスを付けずに入れてください」と出す。

## 7. 画面

### 7.1 撮影の画面（起動したときの画面）

- 背景いっぱいにカメラのプレビュー、中央に照準（M2 と同じ）。
- 上に小さな板：方位角（真北）、仰角、ロール、方位の精度、位置の精度。M2 の詳しい表示（地磁気の強さなど）は「詳細」を押したときだけ出す。
- 警告の帯（あてはまるものだけ）：キャリブレーションの促し（2節）、位置がない・古い、カメラの許可がない、サーバーが未設定、招待コードが違う（401）。
- 下にシャッターのボタン。仰角が 15° 未満なら押せず、「空に向けてください（仰角 15° 以上）」と出す。
- 押したら、撮影 → 0.5 秒たつまで待つ → 保存（3節）→ 「保存しました」と短く表示 → 送信を始める。保存が終わるまでシャッターは押せない。保存に失敗したら、その旨を表示する。
- 二度押しは、`useRef` のフラグで同期的にはじく（状態の更新を待たない）。
- 向きのサンプルは、押した時点で「押した時刻 + 0.5 秒」のタイマーを撮影と並行に走らせ、そのタイマーで前後 0.5 秒のサンプルを複製しておく（撮影に時間がかかっても、押した瞬間のサンプルが手元の2秒分の記録から消えないように）。撮影と、このタイマーの両方を待ってから保存する。
- 押してから `takePictureAsync` が返るまでの時間（ミリ秒）を測り、「保存しました（撮影 123 ms）」のように表示する（8.2 の確認のため）。
- 右上に「未送信 N 件」と、設定・送信状況の画面へのボタン。

### 7.2 設定・送信状況の画面

- サーバーの URL と招待コードの入力（招待コードは伏せ字）。「保存」で `normalizeServerUrl` を通して保存する。保存したら、止めていたキューを再開する（4.2）。
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

1 のメタデータには、向き（M2 のテストと同じ回転行列のサンプル）、位置、EXIF 一式を入れ、全項目がサーバーの検証を通ることを確かめる。

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
4. **仰角の制限**：地平線に向けるとシャッターが押せず、15° 以上で押せる。
5. **機内モード**：機内モードで3枚撮る。「未送信 3 件」になる。
6. **電波が戻ったとき**：機内モードを切る。アプリを開いたまま、1分以内に3件とも送られる。
7. **アプリを閉じていたとき**：機内モードで撮り、アプリを完全に終了してから機内モードを切り、アプリを開く。起動してすぐ送られる。
8. **サーバーが止まっているとき**：サーバーを止めて撮る。「未送信」のまま、失敗の回数が増える。サーバーを起動し直すと、30 秒〜数分のうちに送られる（待ち時間が延びていたら「今すぐ送る」で即座に送れる）。
9. **招待コードが違うとき**：設定でコードを変えて撮る。「招待コードが違う」と表示され、送られない。正しいコードに戻すと送られる。
10. **キャリブレーション**：PC やスピーカーに近づけると、8の字に振るよう表示される。
11. **撮影にかかる時間**：「保存しました（撮影 N ms）」の N を何回か控える。数百 ms を超えるなら、向きを取る時刻（押した時刻）とシャッターが切れた時刻がずれるので、向きを取る時刻の決め方を見直す。

## 9. 撮影中の向きの記録（実機の確認を受けた見直し）

### 9.1 背景

- 実機（Android、Expo Go）で、シャッターを押してから `takePictureAsync` が返るまでが 497〜1444ms とばらついた。露光はこの間のどこかで起きるが、Expo（SDK 57）の `takePictureAsync` は露光の時刻（CameraX の `ImageInfo.timestamp`）を JS に渡さないので、正確な瞬間はわからない。
- 予測に使う向きの精度は、方位角 ±15〜20°、仰角 ±5〜10° で足りる見込み（設計の議論の第4ラウンド）。端末を止めて撮れば露光の瞬間がいつでも向きはほぼ同じなので、困るのは撮影中に端末を動かしたときだけ。
- そこで次の4つを行う。露光の時刻を取る方法（development build に移って expo-camera にパッチを当てる、EXIF の時刻を使う）は見送る。撮影中に動いた写真が多く、予測への悪影響がわかったら考え直す。そのときに過去の写真も計算し直せるよう、撮影中の向きをすべて記録しておく。

### 9.2 撮影中の表示

- シャッターを押してから `takePictureAsync` が返るまで、「撮影中…動かさないでください」を表示する。

### 9.3 サンプルの時刻

- これまでサンプルの時刻は、JS で受け取ったときの `Date.now()` だった。撮影中は JS のスレッドが忙しく、受け取りが遅れる。
- これからは、DeviceMotion の `rotation.timestamp`（センサーが値を測った時刻。秒。端末の起動からの経過時間で、`Date.now()` とは別の時計）を正とする。
  - `tSensorMs = rotation.timestamp * 1000`
  - 同じ `tSensorMs` のサンプルが続けて届いたら、2つめ以降は捨てる。
  - 時計のずれ `offsetMs = min(受け取ったときの Date.now() − tSensorMs)` を、受け取るたびに更新する（受け取りの遅れが最も小さかったときの値が、本当のずれにいちばん近いため）。
  - サンプルの壁時計の時刻は `t = tSensorMs + offsetMs` とする。`offsetMs` は使う時点の値で計算し直す（サンプルに固定しない）。
- サンプルには、回転行列 `R` に加えて、生の `alpha`・`beta`・`gamma`（ラジアン）と `tSensorMs` を持たせる。
- サンプルを持っておく長さ（`KEEP_MS`）は 10 秒にする（撮影に数秒かかっても、押した 0.5 秒前からの分が残るように）。

### 9.4 撮影の窓と代表の向き

- `pressedAtMs`：シャッターを押した時刻（`Date.now()`）。`captured_at` は今までどおりこの時刻。
- `completedAtMs`：`takePictureAsync` が返った時刻（`Date.now()`）。
- 撮影が終わってから、`t` が `[pressedAtMs − 500, completedAtMs]` に入るサンプルを取り出す（撮影の窓）。
- 代表の向き（今の `orientation` の `azimuth_deg`・`pitch_deg`・`roll_deg`）は、`t` が `(pressedAtMs + completedAtMs) / 2` に最も近いサンプルから計算する。押した時刻ではなく撮影の窓の中ほどを使うのは、露光が窓のどこで起きたかわからないため。
- `orientation.stddev_deg` は、撮影の窓のサンプルで計算する（今の `angularSpreadDeg` と同じ計算）。
- 撮影の窓のサンプルが1つもなければ、今までどおり `orientation` は null。

### 9.5 メタデータに足す項目（`capture`）

`schema_version` は 1 のまま、任意の項目 `capture` を足す。サーバーは、ない場合（古いアプリ）も受け付ける。

```json
"capture": {
  "pressed_at": "2026-10-10T01:23:45.678Z",
  "completed_at": "2026-10-10T01:23:46.663Z",
  "duration_ms": 985,
  "motion_deg": 3.2,
  "sensor_clock_offset_ms": 1791530000123.4,
  "exif_datetime_original": "2026:10:10 10:23:46",
  "exif_subsec_time_original": "512",
  "orientation_trace": {
    "source": "expo-sensors DeviceMotion rotation (alpha, beta, gamma)",
    "samples": [[123456789.0, 0.12, 1.45, -0.03], ...]
  }
}
```

| 項目 | 内容 |
|---|---|
| `pressed_at` / `completed_at` | 9.4 の時刻（UTC、ミリ秒） |
| `duration_ms` | `completedAtMs − pressedAtMs`（整数、0 以上） |
| `motion_deg` | 撮影の窓の中で、カメラの向き（カメラ方向ベクトル）が最も離れた2つのサンプルのなす角（度、0〜180）。サンプルが2つ未満なら null。なす角は `atan2(|a×b|, a·b)` で求める |
| `sensor_clock_offset_ms` | 9.3 の `offsetMs`。まだサンプルが届いていなければ null |
| `exif_datetime_original` / `exif_subsec_time_original` | 写真の EXIF の `DateTimeOriginal`・`SubSecTimeOriginal` を、そのままの文字列で（なければ null）。露光の時刻を割り出せるか、あとで調べるため |
| `orientation_trace.source` | サンプルの出どころ（文字列、200 文字まで） |
| `orientation_trace.samples` | 撮影の窓のサンプル。1件は `[tSensorMs, alpha, beta, gamma]`（ミリ秒・ラジアン）。時刻の古い順。最大 1000 件 |

- `orientation_trace` は、撮影の窓のサンプルがなければ null。
- 角度に直す前の値を残すのは、露光の時刻があとでわかったときに、どのサンプルを使うかを選び直し、同じ計算で向きを求め直せるようにするため。
- サーバーは保存するだけで、`motion_deg` などで受け付けを判断しない。学習で使う基準（例：10° 未満はそのまま、10〜30° は印、30° 以上は向きを欠損扱い）は、データが溜まってから分布を見て決める。

### 9.6 サーバーの検証（`server/src/sky_server/models.py`）

- `ObservationMetadata` に `capture: Capture | None = None` を足す（キーがなくてもよい唯一の項目）。
- `Capture`（知らない項目は 422）
  - `pressed_at`・`completed_at`：タイムゾーン付きの時刻。`completed_at >= pressed_at`。
  - `duration_ms`：0 以上 600000 以下の整数。
  - `motion_deg`：0 以上 180 以下、または null。
  - `sensor_clock_offset_ms`：有限の数、または null。
  - `exif_datetime_original`・`exif_subsec_time_original`：64 文字までの文字列、または null。
  - `orientation_trace`：null、または `{source: 1〜200 文字, samples: 0〜1000 件の [有限の数 ×4]}`。
