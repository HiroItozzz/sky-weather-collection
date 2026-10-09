# Sky Weather（スマホアプリ）

空の写真とメタデータを集めるアプリ。Expo（React Native、TypeScript）、Expo SDK 57。

いま（M3）は、空の写真を撮って、向き・位置・EXIF などのメタデータと一緒に端末内に保存し、サーバー（`server/`）へ送る。送れないときは端末内に溜めておき、電波が戻ったときやアプリを開いたときに送り直す。

- 向きと位置の計算：[`docs/m2-app-sensors.md`](../docs/m2-app-sensors.md)
- 撮影・保存・送信：[`docs/m3-app-capture.md`](../docs/m3-app-capture.md)（8節に実機で確かめる手順）

## サーバーにつなぐ

1. PC でサーバーを起動し、撮影者を作る（`server/README.md` を参照）。端末から届くように `--host 0.0.0.0` を付ける。

   ```bash
   cd server
   uv run python -m sky_server.admin create-user --name <名前>   # 招待コードが1回だけ表示される
   uv run uvicorn sky_server.main:create_app --factory --host 0.0.0.0 --port 8000
   ```

2. アプリの右上のボタンから設定の画面を開き、サーバーの URL（`http://<PC の LAN の IP>:8000`）と招待コードを入れて「保存」、「接続を確かめる」。
3. 空に向けて（仰角 20° 以上）シャッターを押す。

## 必要なもの

- PC：Node.js 20 以上（22 で確認）と npm。Windows・macOS・Linux のどれでもよい。
- Android 端末：Google Play から「Expo Go」をインストールしておく。Expo Go は最新の SDK（いまは 57）にしか対応しないので、古い Expo Go は更新する。
- PC と端末が同じ Wi-Fi につながっていること（つながらないときは下の「つながらないとき」を参照）。

## 起動のしかた

```bash
cd app
npm install
npx expo start
```

1. ターミナルに QR コードが表示される。
2. 端末で Expo Go を開き、「Scan QR code」で読み取る。
3. アプリが開いたら、カメラと位置情報の許可を求められるので許可する。

コードを保存すると、端末の画面が自動で更新される。止めるときはターミナルで Ctrl+C。

### つながらないとき

- PC のファイアウォールが Metro（ポート 8081）をふさいでいないか確かめる。
- 同じ Wi-Fi にできない場合や、会社・学校のネットワークの場合は、トンネル経由で起動する。

  ```bash
  npx expo start --tunnel
  ```

  初回は `@expo/ngrok` のインストールを聞かれるので、`y` で入れる。

- USB でつなぐ場合は、端末の「USB デバッグ」をオンにし、PC に Android SDK Platform-Tools（`adb`）を入れてから `npx expo start --android`。

## 開発用のコマンド

```bash
npm test              # jest（向きの計算、メタデータ、再送キューなどのテスト）
npm run test:e2e      # 動いているサーバーに対するテスト（SKY_E2E_URL と SKY_E2E_TOKEN が必要。docs/m3-app-capture.md の 8.1）
npx tsc --noEmit      # 型チェック
npx expo-doctor       # 依存パッケージと設定の診断
npx expo install <パッケージ>   # パッケージの追加（SDK に合う版が選ばれるので npm install ではなくこちらを使う）
```

## 構成

```
App.tsx               撮影の画面と設定の画面の切り替え
src/screens/          撮影の画面、設定・送信状況の画面
src/useSensors.ts     センサーと位置の購読
src/exif.ts           EXIF から camera の項目を作る
src/metadata.ts       サーバーに送るメタデータ（設計メモ4節）を作る
src/observationStore.ts  端末内への保存（画像、metadata.json、status.json）
src/transport.ts      サーバーへの PUT と、応答の分類
src/uploadQueue.ts    再送キュー（送る順番、待ち時間、止めどころ）
src/useUploadQueue.ts 再送のきっかけ（起動、前面、電波、30秒ごと）
src/settings.ts       サーバーの URL と招待コード（expo-secure-store）
src/orientation.ts    回転行列からカメラの方位角・仰角・ロールを計算する
src/declination.ts    WMM2025 による偏角と全磁力
src/record.ts         向きと位置を、設計メモ4節の orientation / location の形にする
```

## メモ

- Expo Go で動く範囲だけで作っている。ネイティブのモジュールが必要になったら、開発用ビルド（`npx expo run:android` か EAS Build）に切り替える。
- `android/`・`ios/` のディレクトリは自動で生成されるもので、リポジトリには入れない（`.gitignore` 済み）。
