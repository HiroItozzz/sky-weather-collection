# M5 のアプリの仕様：予想、撮影時の天気、タイムライン、記録（とセキュリティの見直し）

設計メモ13節（M5）のうちアプリ側（13.1、13.6）と、セキュリティの見直しで出た2件をまとめて作る。サーバーの API（13.5）はサーバー担当が並行して作っているので、13.5 の JSON の形を正として、テストでは偽の応答を使う。

M3 の仕様（`docs/m3-app-capture.md`）はそのまま使う。

## 1. セキュリティの見直し

### 1.1 リリースビルドでは https だけを許す

- `normalizeServerUrl(input, { allowHttp })` に引数を足す。`allowHttp` が false なら `https://` で始まる URL だけを受け付け、`http://` は null（エラー）にする。
- 画面からは `allowHttp: __DEV__` で呼ぶ（Expo Go や `expo start` での開発中は http も可。リリースビルドでは https だけ）。
- 保存済みの設定を読むとき（`loadSettings`）も同じ検査をかけ、通らない URL は未設定（空文字列）として扱う。開発中に http を保存した端末でリリースビルドに入れ替えても、平文で送らないようにするため。
- エラーの文言（リリースビルド）：「サーバーの URL は https:// で始めてください。」

### 1.2 送信済みのメタデータを 30 日で消す

- `metadata.json` には小数第7位の位置（約1cm）が入っている。サーバーには正確な値が残るので、端末に残し続ける必要はない。
- 起動時の掃除で、`status.json` が `sent` で、`sent_at` から 30 日以上たったものは、ディレクトリ（`observations/{id}/`）ごと消す。
- `sent_at` がない・読めない `sent` は消さない（判断できないものは残す）。`pending` と `rejected` は今までどおり残す。
- サムネイル（3節）は別のディレクトリにあるので、消えずに残る。
- 判定は純粋な関数 `shouldPurgeSent(status, nowMs)` にしてテストする。

## 2. 予想の回答（13.1）

- 撮影画面のシャッターの上に、「降る」「降らない」「わからない」の3つの切り替えを置く。初期値は「わからない」（null）。
- 選んだ値は、シャッターを押した時点で固定して `buildMetadata` に渡し、`user_guess` に入れる（`"rain"` / `"no_rain"` / null）。
- **1枚撮るごとに「わからない」に戻す**。前の回答が残ったまま次を撮ると、考えずに同じ答えが入り、予想のデータが偏るため。
- 説明の文言：「1時間後に、ここで雨が降ると思いますか？」

## 3. サムネイル

- 送信が成功して元の画像を消すときに、長い辺 320px の縮小版（JPEG、品質 0.7）を `Paths.document/thumbnails/{observation_id}.jpg` に残す。
- 観測のディレクトリとは別に置く。1.2 でディレクトリごと消しても残すため。また、M3 の掃除（`metadata.json` のないディレクトリを消す）に巻き込まれないため。
- 縮小には `expo-image-manipulator` を使う（追加する依存。Expo Go に含まれていて、Expo の公式のモジュールなので選んだ）。元の画像を `ImageManipulator.manipulate(uri).renderAsync()` で読み込んで幅と高さを知り、長い辺が 320px になるように `resize` する。
- 元の JPEG は EXIF の `Orientation` で向きを表している（M3 の 1.1）。`expo-image-manipulator` は Android では `expo-image-loader`（Glide）で読み込むので、EXIF の向きが反映されたビットマップになるはずだが、確かめていない（実機で確かめる。7節）。
- 作り方の順番：`status.json` を `sent` にする → サムネイルを作る → 元の画像を消す（`Store.deleteImage` の中でサムネイルを作ってから消す。キューの段取りは変えない）。
- サムネイルの作成に失敗しても、元の画像は消す（送信済みの 3〜6MB を残し続けないため）。失敗は console.warn に出し、タイムラインでは代わりの表示にする。
- 起動時の掃除で、`sent` なのに `image.jpg` が残っているもの（サムネイルを作る前に落ちたもの）は、サムネイルを作ってから元の画像を消す。
- M5 より前に送ったものにはサムネイルがない。代わりの表示にする。

## 4. API の呼び出し（`app/src/api.ts`）

13.5 の3つの API を呼ぶ。`fetch` は引数で受け取る（テストで差し替えるため。また、jest-expo はグローバルの `fetch` を差し替えるため）。

```ts
type WeatherAtCapture = { category: "clear" | "cloudy" | "rain"; weather_code: number; temperature_c: number | null; precipitation_mm: number | null; cloud_cover_pct: number | null };
type ObservationView = {
  observation_id: string; received_at: string; captured_at: string;
  user_guess: "rain" | "no_rain" | null;
  weather_at_capture: WeatherAtCapture | null;
  answer: { result: "rain" | "no_rain" | "unknown"; source: "amedas" | "open_meteo" | null };
  correct: boolean | null;
};
type ObservationPage = { observations: ObservationView[]; next_before: string | null };
type Stats = { observations_total: number; answered_total: number; guesses_total: number; guesses_correct: number; by_category: { clear: number; cloudy: number; rain: number; unknown: number } };
type ApiResult<T> = { kind: "ok"; data: T } | { kind: "not_found" } | { kind: "auth" } | { kind: "error"; error: string };
```

- `getObservation(settings, id)`、`listObservations(settings, { limit, before })`、`getStats(settings)`。
- 一覧の応答の配列の名前は 13.5 に書かれていないので、`observations` とする（サーバー担当と合わせる。ずれていたら直す）。
- 応答は型を検査してから返す。必要な項目がない・型が違うときは `error`（「サーバーの応答の形が想定と違います」）。知らない項目は無視する（サーバーが項目を足しても壊れないように）。
  - `weather_at_capture` の `temperature_c` などの数値は null も許す（13.5 の例には null がないが、Open-Meteo の値が欠けることはある）。
- 401 は `auth`、404 は `not_found`、それ以外の失敗（通信のエラー、5xx など）は `error`。打ち切りは 15 秒。
- `before` は URL エンコードする（`+` を含む時刻がそのまま送られないように）。

## 5. 撮影時の天気の表示（13.6 の撮影画面）

- 保存して「保存しました」を出したあと、その観測について `getObservation` を 5 秒ごとに問い合わせ、`weather_at_capture` が取れたら「撮影時の天気：くもり 18.2℃ 雲量 90%」のように表示する。
- 最初の問い合わせから 60 秒たっても取れなければ、何も出さずにやめる。404（まだ送れていない）や通信のエラーも、60 秒の間は問い合わせを続ける。401 ならやめる。
- 次のシャッターを押したら、前の問い合わせはやめる。画面を離れたときもやめる。
- 段取りは純粋な関数 `pollWeather({ get, sleep, now, intervalMs, timeoutMs, signal })` にしてテストする。
- 分類の表示：`clear` は「晴れ」、`cloudy` は「くもり」、`rain` は「雨」。値が null の項目は表示しない。

## 6. 画面

### 6.1 画面の切り替え

- 今の作り（`App.tsx` の状態で切り替える。React Navigation などのライブラリは使っていない）に合わせて、`"capture" | "timeline" | "stats" | "settings"` を切り替える。ライブラリは増やさない（画面が4つで、戻る操作も単純なため）。
- 撮影画面の上部に「タイムライン」「記録」「設定」のボタンを置く。ほかの画面は上部に「← 撮影に戻る」を置く。Android の戻るボタン（`BackHandler`）でも撮影画面に戻る。

### 6.2 撮影画面

- 2節の予想の切り替え、5節の天気の表示を足す。ほかは M3 のまま。

### 6.3 タイムライン画面

- `listObservations(limit 50)` で取った一覧を、新しい順に表示する。下に「もっと見る」（`next_before` があるときだけ。押すと続きを足す）。
- 1件の表示：サムネイル（なければ代わりの四角）、撮影日時（端末のタイムゾーン、`2026/10/09 14:05` の形）、予想、答え、当たり外れ、撮影時の天気。
  - 予想：「降る」「降らない」「予想なし」。
  - 答え：`rain` は「降った」、`no_rain` は「降らなかった」、`unknown` は「答え合わせ待ち（撮影の約6〜7時間後）」。
  - 当たり外れ：`correct` が true は「当たり」、false は「はずれ」、null は表示しない。
- 一覧の上に、端末に未送信のものがあれば「未送信 N 件（送れたら一覧に出ます）」。
- 引っぱって更新（`RefreshControl`）。

### 6.4 記録画面

- `getStats` の結果を表示する：送った枚数（`observations_total`）、答え合わせが済んだ枚数（`answered_total`）、予想の的中率（`guesses_correct / guesses_total`、`guesses_total` が 0 なら「まだありません」）、天気の分類ごとの枚数（晴れ・くもり・雨・不明）。
- 曇りと雨の写真が少ないとき、「曇りや雨の日の写真は特に貴重です」と添える。少ないの定義：`cloudy + rain` が `clear + cloudy + rain` の 30% 未満（`clear + cloudy + rain` が 0 のときも添える）。
- 的中率などの計算と、この判定は純粋な関数にしてテストする。

### 6.5 通信できないとき・未設定のとき

- タイムラインの最初のページと記録は、取得に成功するたびに `Paths.document/cache/timeline.json`・`stats.json` に保存する（位置は含まれない）。
- 取得に失敗したら、保存した内容を表示し、「通信できないため、前回取得した内容を表示しています」と出す。保存した内容もなければ、エラーの文言だけ出す。
- 401 なら「招待コードを確かめてください」、サーバーが未設定なら「設定の画面でサーバーを設定してください」と出す。

## 7. 確かめること

### 7.1 自動のテスト（jest）

- `normalizeServerUrl` の `allowHttp`、`loadSettings` の検査を通す部分（純粋な関数に分ける）。
- `shouldPurgeSent`（30 日ちょうどの境界、`sent_at` なし、`pending` と `rejected`）。
- `buildMetadata` が `user_guess` を入れること。
- `api.ts`：13.5 の例の JSON を偽の応答にして、3つの API が型どおりに返すこと。401・404・5xx・通信のエラー・形の違う応答・null を含む `weather_at_capture`・`before` のエンコード。
- `pollWeather`：すぐ取れる、何回か 404 のあとに取れる、60 秒で諦める、401 でやめる、途中で止められる。
- 表示用の関数（分類の名前、答えの文言、的中率、「特に貴重です」の判定、日時の形）。
- e2e は、サーバー側の 13.5 がマージされてから足す（今回は足さない）。

### 7.2 実機で確かめる手順（ユーザー向け）

サーバーの 13.5 ができてから行う。

1. **予想**：「降る」を選んで撮ると、サーバーのメタデータの `user_guess` が `rain` になる。撮ったあと切り替えが「わからない」に戻る。
2. **撮影時の天気**：撮って1分以内に「撮影時の天気」が出る（サーバーの天気の取得が動いているとき）。
3. **サムネイル**：送信できたあと、タイムラインにサムネイルが出る。向きが正しい（空が上）。縦持ちと横持ちの両方で撮って確かめる。
4. **タイムライン**：撮ったものが新しい順に並ぶ。答え合わせの前は「答え合わせ待ち」、6〜7時間後に答えと当たり外れが出る。
5. **記録**：枚数と的中率が、タイムラインと合っている。
6. **オフライン**：機内モードでタイムラインと記録を開くと、前回の内容と「通信できないため…」が出る。
7. **https**（リリースビルドを作ったとき）：設定で `http://` の URL が保存できない。
