# M6：アプリの自動更新（EAS Update）と、招待の QR コード

友達に APK を配る前に、2つを入れる。

- A. EAS Update：JavaScript の修正を、APK を入れ直さずに届ける。
- B. 招待の QR コード：サーバーの URL と招待コードを、読み取るだけで設定できるようにする。

どちらもネイティブの部分（パッケージの追加、`scheme`）を変えるので、入れたあとに APK を1回作り直して配る。

## 1. EAS Update

### 1.1 設定

- `expo-updates` を入れる（SDK 57 の版 `~57.0.25`。`npx expo install` が使えなければ `npm install expo-updates@~57.0.25`）。
- `app.json`
  - `expo.updates.url`：`https://u.expo.dev/07639bc8-b6b7-497a-aeab-3d9647d8e36f`（EAS のプロジェクト ID）
  - `expo.runtimeVersion`：`{"policy": "fingerprint"}`。ネイティブの部分（パッケージ、`app.json` のネイティブの設定）から自動で計算される。ネイティブの部分が変わると値が変わり、古い APK には新しい更新が届かない（壊れた組み合わせを防ぐ）。
  - `android.versionCode` を 3 にする。
- `eas.json` の `build.preview` に `"channel": "preview"` を足す。
- 更新の取り込み方は `expo-updates` の既定のまま：起動時に確かめて裏で取ってきて、次の起動から使う。アプリのコードで更新を扱う処理は書かない。

### 1.2 設定画面の表示

設定画面の下に、いま動いている版を小さく表示する（不具合の報告のときに、どの版かを聞けるように）。

- `アプリ {expo.version}（{versionCode}）`
- `更新 {Updates.updateId の先頭8文字}（{Updates.createdAt を端末の時刻で YYYY-MM-DD HH:mm}）`。更新を使っていない（APK に入っている JS で動いている）ときは `更新 なし（APK のまま）`。Expo Go や開発中で `Updates.isEnabled` が false のときは、この行を出さない。

### 1.3 配り方（README に書く）

```bash
cd app
npx eas-cli@latest update --channel preview --message "変更の説明"
```

- ネイティブの部分を変えたとき（パッケージの追加、`app.json` の `android` や `plugins` の変更など）は、update ではなく APK を作り直して配る。`fingerprint` が変わるので、古い APK には届かない。
- 更新は、端末でアプリを2回起動すると反映される（1回目で取ってきて、2回目から使う）。

## 2. 招待の QR コード

### 2.1 設定の形（設定リンク）

```
skyweather://setup?server={サーバーの URL}&code={招待コード}
```

- 値は URL エンコードする。
- `app.json` に `expo.scheme: "skyweather"` を足す。これで、端末のカメラアプリで QR を読むと、このアプリが開く。

### 2.2 アプリ：設定リンクの読み取り

純粋な関数 `parseSetupLink(text: string): {serverUrl: string, inviteCode: string} | {error: string}` を `src/setupLink.ts` に作る。

- スキームが `skyweather:`、パス（ホスト）が `setup` でなければエラー。
- `server` と `code` の両方がなければエラー。
- `server` は `https://` で始まること（`http://` はエラー。QR で受け取る URL は暗号化された通信に限る）。末尾の `/` は取る。`new URL()` で読めなければエラー。
- `code` は前後の空白を取り、空ならエラー。長さが 200 文字を超えたらエラー。
- エラーの文言は利用者に見せる日本語（例：「このQRコードは Sky Weather の設定用ではありません」「サーバーの URL は https:// で始まる必要があります」）。

### 2.3 アプリ：読み取りの入口（2つ）

1. 設定画面に「QRコードで設定」のボタン。押すと、`expo-camera` の `CameraView`（`barcodeScannerSettings={{ barcodeTypes: ["qr"] }}`）で読み取る画面を出す。最初に読めた1つで閉じる（同じ QR を何度も処理しない）。「やめる」で閉じられる。
2. 設定リンクでアプリが開いたとき（React Native の `Linking.getInitialURL()` と `Linking.addEventListener("url")`）。`App.tsx` で受け取り、設定画面に渡す。

どちらも、すぐには保存しない。確認のダイアログを出す。

- 本文：`このサーバーに写真と位置を送るように設定します。\n{serverUrl}\n心当たりのない QR コードなら「やめる」を押してください。`
- ボタン：「設定する」「やめる」
- 「設定する」で、今の手入力の保存と同じ処理で保存し、続けて「接続を確かめる」と同じ確認を行い、結果を表示する。
- 理由：知らない人の QR やリンクで送信先を書き換えられると、写真と位置と招待コードが他人のサーバーに送られてしまうため。

読み取ったテキストが設定リンクでなければ、`parseSetupLink` のエラーの文言を表示する。

手入力の欄（`http://` も入れられる）は、開発用にそのまま残す。

### 2.4 サーバー：管理コマンドで QR を出す

`create-user` に、省略できる引数 `--setup-server-url URL` を足す。

- 付けたときは、今の出力（`user_id: ...` と `招待コード（この1回だけ表示されます）: ...` の2行。CI がこの形を読むので変えない）のあとに、次を表示する。
  - `設定リンク: skyweather://setup?server=...&code=...`
  - 端末に表示できる QR コード（Python の `qrcode` パッケージの `print_ascii(invert=True)` など、文字で描くもの）
- `--setup-server-url` が `https://` で始まらなければ、撮影者を作る前にエラーで終了する（終了コード 2、argparse のエラー）。末尾の `/` は取る。
- 設定リンクを作る関数（`build_setup_link(server_url, token) -> str`）は、テストできるように分ける。
- `qrcode` は通常の依存に足す（`uv add qrcode`）。QR の画像（PNG）は作らない（ファイルに招待コードが残らないように）。
- QR コードには招待コードが入る。画面ごと人に見せない、写真を撮って送らない、を `docs/deploy-gcp.md` の撮影者の作成のところに一言書く。

## 3. 完成条件

- `cd app && npx tsc --noEmit && npx jest` が通る。`parseSetupLink` のテスト（正しいリンク、スキーム違い、`setup` 以外、`http://`、`server` なし、`code` なし、空白だけの `code`、長すぎる `code`、エンコードされた値、末尾の `/`）。
- `cd server && uv run ruff check && uv run ruff format --check && uv run pytest -q` が通る。`build_setup_link` のテストと、`--setup-server-url` に `http://` を渡すと撮影者が作られないことのテスト。
- `npx expo config --type public` で、`updates.url`、`runtimeVersion`、`scheme` が読める。
