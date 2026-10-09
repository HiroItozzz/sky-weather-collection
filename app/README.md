# Sky Weather（スマホアプリ）

空の写真とメタデータを集めるアプリ。Expo（React Native、TypeScript）、Expo SDK 57。

いま（M2）は、端末の向き（方位角・仰角・ロール）と位置を表示して、正しく取れているかを実機で確かめるための画面だけがある。仕様と、実機で確かめる項目は [`docs/m2-app-sensors.md`](../docs/m2-app-sensors.md) を参照。

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
npm test              # jest（向きの計算などのテスト）
npx tsc --noEmit      # 型チェック
npx expo-doctor       # 依存パッケージと設定の診断
npx expo install <パッケージ>   # パッケージの追加（SDK に合う版が選ばれるので npm install ではなくこちらを使う）
```

## 構成

```
App.tsx               画面（カメラのプレビューと数値の表示、「記録」ボタン）
src/useSensors.ts     センサーと位置の購読
src/orientation.ts    回転行列からカメラの方位角・仰角・ロールを計算する
src/declination.ts    WMM2025 による偏角と全磁力
src/record.ts         「記録」の JSON（設計メモ4節の orientation / location の形）を作る
```

## メモ

- Expo Go で動く範囲だけで作っている。ネイティブのモジュールが必要になったら、開発用ビルド（`npx expo run:android` か EAS Build）に切り替える。
- `android/`・`ios/` のディレクトリは自動で生成されるもので、リポジトリには入れない（`.gitignore` 済み）。
