# M2 の仕様：センサーの値を表示する画面

M2 では、端末の向き（カメラの方位角・仰角・ロール）と位置が正しく取れるかを、実機で確かめるための画面を作る。撮影・端末内への保存・送信は M3 で扱うので、ここでは作らない。

この文書は、調べた結果（2026-10、Expo SDK 57 のソースを読んで確認）と、それにもとづく判断、実装の仕様をまとめたもの。

## 1. 調べた結果

### 1.1 Expo の DeviceMotion（expo-sensors）は Android で何を返すか

`expo-sensors` 57.0.3 の `DeviceMotionModule.kt` を読んで確認した。

- 向きは Android の `Sensor.TYPE_ROTATION_VECTOR` から取っている。これは加速度・ジャイロ・地磁気を OS（またはセンサーハブ）が融合したもので、Android のドキュメント上、世界座標は次のとおり。
  - X：東、Y：磁北、Z：天頂（いわゆる ENU。ただし Y は **磁北** で、真北ではない）
- 端末座標は Android の標準どおり。X：画面の右、Y：画面の上（縦持ちで端末の上端の方向）、Z：画面から手前（ユーザーの方向）。背面カメラは -Z を向く。
- 回転ベクトルから `SensorManager.getRotationMatrixFromVector` で回転行列 R（端末座標 → 世界座標）を作り、`SensorManager.getOrientation` でオイラー角に直してから JS に渡している。回転行列そのものや四元数は JS に渡されない。
- JS に届く `rotation` は次の符号の付け替えをしたもの（単位はラジアン）。
  - `alpha = -azimuth`、`beta = -pitch`、`gamma = roll`（右辺は `getOrientation` の値）
- `getOrientation` の定義は `azimuth = atan2(R[1], R[4])`、`pitch = asin(-R[7])`、`roll = atan2(-R[6], R[8])`（R は行優先の 3×3）。
- 回転ベクトルのセンサーが出す精度（`values[4]` の推定誤差、`onAccuracyChanged` のキャリブレーション状態）は JS に渡されない（`onAccuracyChanged` は空の実装）。
- `isAvailableAsync()` は、ジャイロ・加速度・線形加速度・回転ベクトル・重力の5つのセンサーがすべてあるときだけ true を返す。
- センサーの登録は `SENSOR_DELAY_FASTEST`。JS への送信は画面の描画ごと（Choreographer）で、`setUpdateInterval(ms)` で間引ける。
- 画面の回転（縦・横）は `orientation` として別に渡されるだけで、`rotation` の値は画面の回転に影響されない（端末座標のまま）。

### 1.2 オイラー角から回転行列を戻せるか

JS には回転行列ではなくオイラー角しか届かないので、ここから R を作り直せるかを確かめた。

- `R = Rz(alpha) · Rx(beta) · Ry(gamma)` で、`getOrientation` に入る前の R が復元できる（W3C の DeviceOrientation と同じ式）。ランダムな回転 10 万個で、要素の誤差の最大は 1e-13 程度だった。
- 心配なのは `beta = ±90°`（端末を縦に立て、カメラを水平に向けた姿勢）。ここでは `alpha` と `gamma` が個別には決まらない（ジンバルロック）。しかし、値が float32 で丸められる前提で模擬計算すると、R を作り直したときのカメラ方向の誤差は最悪でも約 0.01° で、センサー自体の誤差（数度）に比べて無視できる。`alpha` と `gamma` は同じ R から計算されているので、組み合わせとしては正しい R に戻るため。
- したがって、JS 側で R を作り直してから、カメラ方向ベクトルで角度を計算する方式で問題ない。オイラー角（`alpha` など）をそのまま方位や仰角として使うことはしない。

### 1.3 方位のもう一つの候補：expo-location の `watchHeadingAsync`

`expo-location` 57.0.20 の `LocationModule.kt` を読んで確認した。採用しない（偏角と精度の参考にだけ使う。後述）。

- 加速度と地磁気の生の値から `SensorManager.getRotationMatrix` → `getOrientation` で方位を出している。ジャイロを使わないので、値がばらつきやすい。
- 方位は端末の **上端（Y 軸）** の方向で、カメラの方向ではない。空に向けて端末を後ろに倒すと、上端は撮影方向と反対側を向くので、方位が 180° ずれる。
- 方位が約 2° 以上変わったときしか値を送らない。
- `accuracy` は `onAccuracyChanged` の最後の値（0〜3）。加速度と地磁気の両方のセンサーの通知が同じ変数に書き込まれるので、どちらのセンサーの状態なのかは区別できない。
- `trueHeading` は、開始時の端末の位置から Android の `GeomagneticField`（WMM）で求めた偏角を足したもの。

### 1.4 偏角をどう得るか

- Android の `GeomagneticField` を直接呼ぶ API は Expo にない（`watchHeadingAsync` の中で使われているだけ）。
- そこで、npm の `geomagnetism`（0.2.0、Apache-2.0、純粋な JavaScript）を使う。WMM2025（有効期間 2024-11〜2029-11）の係数を含んでいる。ネイティブのコードを含まないので Expo Go でも動き、jest でテストできる。
- 2026-10 時点での計算値：東京 -7.9°、札幌 -9.9°、那覇 -5.9°（西偏が負）。国土地理院の値と同程度（概ね ±0.5° 以内のはず。正確な比較はしていない）。
- WMM は地球全体の主磁場のモデルなので、地殻の局所的な異常や、近くの鉄やスピーカー・磁石入りのケースの影響は含まない。

### 1.5 位置（expo-location）

- `watchPositionAsync` で取る。Android では Google Play 開発者サービスの Fused Location Provider を使う。
- `coords.accuracy` は水平方向の精度（m）。Android の定義では 68% の確率で真の位置がこの半径に入る。
- `coords.altitude` は Android の `Location.getAltitude()` で、**WGS84 楕円体からの高さ**（海抜ではない。日本ではジオイド高の分、およそ 30〜40m 大きい）。また、高度がないときも 0 が入る（`hasAltitude()` を確かめていない）。
- `coords.altitudeAccuracy` は `getVerticalAccuracyMeters()` で、値がないときは 0 になる。
- `timestamp` は位置を測った時刻（UNIX ミリ秒）。

### 1.6 Expo Go で足りるか

- 足りる見込み。`expo-sensors`、`expo-location`、`expo-camera` はすべて Expo Go に含まれていて、`geomagnetism` は純粋な JavaScript なので追加のネイティブのコードがいらない。
- 足りないのは、回転ベクトルの精度とキャリブレーションの状態が取れないこと（1.1）。M2 の実機確認で方位が不安定・不正確だとわかったら、6節の方針どおり、その部分だけネイティブのモジュール（Kotlin）を書く。そのときは開発用ビルド（development build）が必要になり、Expo Go では動かなくなる。

### 1.7 不確かな点

- 機種によっては、`TYPE_ROTATION_VECTOR` の融合のしかたが違う（地磁気の乱れへの強さ、ジャイロのない機種での代替など）。実機で確かめるしかない。
- `watchHeadingAsync` の `accuracy` が地磁気センサーの状態を表しているかは機種しだい（1.3）。
- 背面カメラの光軸が端末の -Z とぴったり一致しているとは限らない（取り付けのずれ）。1° 未満と見込むが、確かめていない。
- iOS は M2 の対象外。参考までに、iOS の DeviceMotion は `CMAttitudeReferenceFrame.xMagneticNorthZVertical`（使えなければ北が任意の座標系）を使い、`alpha` などの意味も Android と同じとは限らない。iOS に対応するときに調べ直す。

## 2. 判断

| 項目 | 判断 | 理由 |
|---|---|---|
| 向きの元データ | DeviceMotion の `rotation` | 融合済みの回転ベクトルなので、生の地磁気より安定している |
| 角度の計算 | `rotation` から R を作り直し、カメラ方向ベクトル（端末の -Z）を世界座標に直してから計算 | 1.2 のとおり誤差は無視でき、真上付近や縦持ちでもオイラー角のような飛びが出ない |
| 偏角 | `geomagnetism`（WMM2025）で、最新の位置から計算 | Expo Go で動き、テストできる |
| `orientation.accuracy` | `watchHeadingAsync` の `accuracy` を対応づける（3: `high`、2: `medium`、1: `low`、0: `unreliable`） | 他に取れる値がない。意味があいまいなことは 1.3 のとおり |
| 地磁気の乱れの目安 | 地磁気センサー（`Magnetometer`）の強さと WMM の全磁力を並べて表示する（記録はしない） | 強さが大きく違えば、近くの鉄や磁石で方位が狂っている疑いがある |
| `location.altitude_m` | `altitudeAccuracy` が null か 0 以下なら null、そうでなければ `altitude`（楕円体高）をそのまま | 0 と「取れない」を区別するため。楕円体高のまま記録することは、下の「監督への確認事項」に挙げる |
| 画面の向き | 縦に固定（`app.json` の `orientation: portrait`） | 計算は端末座標で行うので画面の回転とは無関係だが、表示を簡単にするため |

### 監督への確認事項

- `location.altitude_m` は Android では楕円体高になる。4節に「楕円体高」と書き足すか、海抜に直すか（直すにはジオイドのモデルが必要）。
- 真上付近では方位角とロールがそれぞれ大きくぶれるが、2つを組み合わせれば姿勢は正しく表せる（3.3）。データセットで画像の回転まで正確に使いたいなら、`orientation` に四元数か回転行列を足すことを検討してほしい（schema_version を上げることになる）。

## 3. 向きの計算の仕様（`app/src/orientation.ts`）

純粋な TypeScript のモジュールにし、React Native には依存させない。

### 3.1 座標と型

- 世界座標は ENU（x：東、y：北、z：天頂）。DeviceMotion から作る R の y は磁北。
- 端末座標は Android の定義（1.1）。
- `Vec3 = readonly [number, number, number]`
- `Mat3 = readonly number[]`（長さ 9、行優先。`R[3*i + j]` が i 行 j 列）。R は端末座標のベクトルを世界座標に直す（`v_world = R · v_device`）。

### 3.2 関数

- `rotationFromDeviceMotion(alpha, beta, gamma): Mat3`
  - 引数は DeviceMotion の `rotation` の値（ラジアン）。`Rz(alpha) · Rx(beta) · Ry(gamma)` を返す。
  - `Rz(a) = [[cos a, -sin a, 0], [sin a, cos a, 0], [0, 0, 1]]`、`Rx(b) = [[1, 0, 0], [0, cos b, -sin b], [0, sin b, cos b]]`、`Ry(g) = [[cos g, 0, sin g], [0, 1, 0], [-sin g, 0, cos g]]`
- `cameraDirection(R): Vec3`
  - 端末の -Z を世界座標に直したもの。`(-R[2], -R[5], -R[8])`
- `cameraAngles(R): { azimuthDeg: number; pitchDeg: number; rollDeg: number }`
  - 方位角は R の世界座標の北を基準にする（DeviceMotion から作った R なら磁北基準）。真北への変換は呼び出す側で行う（3.4）。
  - 詳細は 3.3。
- `toTrueAzimuth(magneticAzimuthDeg, declinationDeg): number`
  - `normalizeDeg(magnetic + declination)`。偏角は東偏を正とする（WMM の `decl` と同じ）。
- `normalizeDeg(deg): number`
  - 0 以上 360 未満に直す。浮動小数点の誤差で 360 になる場合（例：`-1e-15`）も 0 にする。
- `angularSpreadDeg(directions: Vec3[]): number | null`
  - カメラ方向ベクトルの集まりのばらつき。各ベクトルを正規化し、その和を正規化したものを平均方向 m とする。各ベクトルと m のなす角（度）の二乗平均の平方根を返す。
  - 2個未満、または和の長さが 0 のときは null。
  - なす角は `acos` ではなく `atan2(|a × m|, a · m)` で求める（小さい角で精度を落とさないため）。

### 3.3 角度の定義

カメラ方向を c = (cE, cN, cU)、端末の右を r = R·(1,0,0)、端末の上を u = R·(0,1,0) とする。

- 仰角 `pitchDeg = asin(clamp(cU, -1, 1))`。水平が 0、真上が 90、真下が -90。
- 水平成分 `h = hypot(cE, cN)`。
- `h >= 1e-9` のとき（通常）
  - 方位角 `azimuthDeg = normalizeDeg(atan2(cE, cN))`。北 0、東 90。
  - ロール `rollDeg = atan2(-rU, uU)`（rU, uU は r, u の天頂成分）。縦持ちで地平線が水平なら 0。ユーザーから見て端末を時計回りに回すと正（写真の中の地平線は反時計回りに傾く）。範囲は -180 より大きく 180 以下。
- `h < 1e-9` のとき（カメラがちょうど真上か真下。上の式では方位とロールが決まらない）
  - ロールは 0 とする。
  - 方位角は、真上なら -u の方位、真下なら u の方位（`normalizeDeg(atan2(sE, sN))`、s = -sign(cU)·u）。
  - こうすると、「ロール 0 のままカメラを方位 φ に向けて真上（真下）まで傾けた」姿勢と連続になり、方位・仰角・ロールの3つから姿勢を一通りに復元できる。
- 真上付近（h が小さいが 1e-9 以上）では、方位角とロールはそれぞれ大きくぶれるが、同じ R から計算しているので、2つを合わせた姿勢は正しい。ばらつきの指標（`stddev_deg`）にはベクトルのなす角を使うので、真上付近でも影響を受けない。

### 3.4 テストする既知の姿勢

R を明示的に書いたテスト（`cameraAngles` 用）。列は「端末の x, y, z が世界のどちらを向くか」。

| 姿勢 | 端末の x, y, z | 期待値 |
|---|---|---|
| 縦持ち・カメラ北・水平 | 東, 天頂, 南 | 方位 0、仰角 0、ロール 0 |
| 縦持ち・カメラ東・水平 | 南, 天頂, 西 | 方位 90、仰角 0、ロール 0 |
| 縦持ち・カメラ西・水平 | 北, 天頂, 東 | 方位 270 |
| 縦持ち・カメラ南を向き 45° 見上げる | 西, (北·cos45 + 天頂·sin45)…（テスト内で計算） | 方位 180、仰角 45、ロール 0 |
| 縦持ち・カメラ北・時計回りに 30° 回す | テスト内で計算 | 方位 0、仰角 0、ロール 30 |
| 画面を上にして机に置く・上端が北 | 東, 北, 天頂（R = 単位行列） | 仰角 -90、方位 0（真下なので u の方位）、ロール 0 |
| 画面を下にして頭上にかざす・上端が北 | 西, 北, 天底 | 仰角 90、方位 180（真上なので -u の方位）、ロール 0 |
| 真上から 0.5° 手前（カメラ北、仰角 89.5°） | テスト内で計算 | 方位 0、仰角 89.5、ロール 0（飛ばない） |

DeviceMotion の角度からのテスト（`rotationFromDeviceMotion` 用）。

- `(0, 0, 0)` → 単位行列。
- `(0, π/2, 0)` → 縦持ち・カメラ北・水平と同じ R（方位 0、仰角 0）。
- `(-π/2, 0, 0)` → 画面を上にして上端が東（Android の azimuth が 90° のとき alpha は -90°）。u が東を向く。
- 任意の R（ランダムな四元数から作る。乱数の種は固定）について、`getOrientation` の式で `alpha, beta, gamma` を求めてから `rotationFromDeviceMotion` に通すと、元の R に戻る（要素の誤差 1e-9 以内）。

その他

- 方位・仰角・ロールから R を作る補助関数をテストの中に書き、ランダムな方位・仰角（-89.9〜89.9）・ロールで往復させて一致することを確かめる。
- `normalizeDeg`：-10 → 350、360 → 0、720.5 → 0.5、-1e-15 → 0。
- `toTrueAzimuth`：(5, -7.9) → 357.1、(355, 10) → 5。
- `angularSpreadDeg`：同じベクトル3つ → 0、1個 → null、北に対して東西に ±1° 振った2つ → 1。

## 4. 偏角と全磁力（`app/src/declination.ts`）

- `magneticModel(lat, lon, altitudeM, date): { declinationDeg: number; totalIntensityUT: number }`
  - `geomagnetism.model(date).point([lat, lon, altitudeKm])` を使う。高度が null なら 0 km とする。全磁力 f は nT なので µT に直す。
  - 期限切れ（WMM2025 の有効期間外）のときも例外にせず、最新のモデルで計算する。ライブラリの既定では例外になるので、`allowOutOfBoundsModel: true` を指定する（このとき `console.error` に警告が出る）。
- テスト：2026-10-09 の東京（35.68, 139.77）で偏角が -8.4〜-7.4、全磁力が 44〜48 µT。札幌（43.06, 141.35）の偏角が東京より西に大きい。

## 5. 記録の JSON（`app/src/record.ts`）

「記録」ボタンを押した瞬間の値を、4節の形にして画面に表示する（送信も保存もしない）。

```json
{
  "captured_at": "2026-10-09T03:12:45.123Z",
  "orientation": {
    "azimuth_deg": 123.45,
    "pitch_deg": 56.78,
    "roll_deg": -1.23,
    "declination_deg": -7.92,
    "accuracy": "high",
    "stddev_deg": 0.42
  },
  "location": {
    "lat": 35.6812362,
    "lon": 139.7671248,
    "accuracy_m": 12.3,
    "altitude_m": 45.6,
    "fix_time": "2026-10-09T03:12:44.000Z"
  }
}
```

- `buildRecord(input): Record` を純粋な関数として作る。入力は、押した時刻、押した時刻の前後 0.5 秒のサンプル（時刻と R）、最新の位置（なければ null）、最新の heading の accuracy（なければ null）。
- `captured_at` は押した時刻（UTC、ミリ秒まで、`toISOString()`）。
- `orientation` の各値は、押した時刻にいちばん近いサンプルの R から計算する。サンプルがなければ `orientation` 自体を null にする。
  - `declination_deg` は位置から 4 で計算する。位置がなければ null。
  - `azimuth_deg` は真北基準。偏角が null なら null（磁北の値を真北として記録しない）。
  - `stddev_deg` は前後 0.5 秒のサンプル全部のカメラ方向で `angularSpreadDeg`。
  - `accuracy` は 2節の対応づけ。値がないか 0〜3 以外なら null。
- `location` は最新の位置。なければ null。
  - `altitude_m` は 2節のとおり。`fix_time` は位置の `timestamp` を ISO 形式にしたもの。
- 丸め：角度は小数第2位、`lat` / `lon` は小数第7位、`accuracy_m` と `altitude_m` は小数第1位。`azimuth_deg` は丸めた後にもう一度 `normalizeDeg` を通す（359.996 が 360.00 にならないように）。
- テスト：サンプルが前後にあるとき最も近いものが選ばれる、位置なしで `declination_deg` と `azimuth_deg` が null、サンプルなしで `orientation` が null、`altitudeAccuracy` が 0 で `altitude_m` が null、359.996 の丸め、accuracy の対応づけ。

## 6. 画面（`app/App.tsx` とフック）

- 起動時にカメラ（`useCameraPermissions`）と位置（`requestForegroundPermissionsAsync`）の許可を求める。拒否されたら、その旨と、設定から許可する必要があることを表示する。カメラが拒否されても、センサーの値は表示する。
- 背景いっぱいに背面カメラのプレビュー（`CameraView`、`facing="back"`）。中央に十字の照準。
- その上に半透明の板を重ね、次をリアルタイムに表示する（数値は小数第1位、更新は約 10 回/秒）。
  - 方位角（真北）と磁北の方位角、仰角、ロール
  - 方位の精度（`accuracy`）、偏角
  - 地磁気の強さ：実測（`Magnetometer` のベクトルの長さ、µT）と WMM の全磁力
  - 緯度・経度、水平精度、高度、位置を測ってからの経過秒数
  - DeviceMotion が使えないとき（`isAvailableAsync()` が false）はその旨
- 「記録」ボタン：押した時刻を覚え、0.5 秒待ってから `buildRecord` を呼び、結果の JSON を整形して重ねて表示する（閉じるボタン付き）。待っている間はボタンを押せなくする。
- センサーの購読
  - DeviceMotion：`setUpdateInterval(20)`（約 50 回/秒）。受け取るたびに R を作り、直近 2 秒分を時刻（`Date.now()`）と一緒に配列に持つ。
  - `Magnetometer`：`setUpdateInterval(100)`。
  - 位置：`watchPositionAsync({ accuracy: Accuracy.BestForNavigation, timeInterval: 1000, distanceInterval: 0 })`。
  - heading：`watchHeadingAsync` を accuracy を得るためだけに使う。
  - 画面を離れるとき（アンマウント）にすべて解除する。
- `DeviceMotion.requestPermissionsAsync()` は呼ばない（Android では ACTIVITY_RECOGNITION を求めるだけで、センサーを読むのに許可は要らない）。

## 7. プロジェクトの構成

- `app/` に `create-expo-app` の `blank-typescript` テンプレート（Expo SDK 57）で作る。Expo Router は使わない（画面が1つなので）。
- 依存：`expo-camera`、`expo-location`、`expo-sensors`、`geomagnetism`。開発用：`jest`、`jest-expo`、`@types/jest`。
- `npm test` で jest を流す（`preset: jest-expo`）。
- `app.json`：名前を「Sky Weather」、`orientation: portrait`。`expo-camera` と `expo-location` の config plugin に、許可を求めるときの説明文（日本語）を書く（Expo Go では使われないが、将来のビルドのため）。
- 完成条件：`npx tsc --noEmit`、`npm test`、`npx expo-doctor`（通信できる範囲で）が通る。

## 8. ユーザーが実機で確かめるチェックリスト

準備：屋外か窓際で、金属の机や PC から 1m 以上離れる。磁石付きの手帳型ケースは外す。端末を8の字に数回振ってから始める。

1. **起動**：Expo Go でアプリが開き、カメラのプレビューと数値が表示される。カメラと位置の許可を求められる。
2. **水平**：端末を縦に持ち、カメラを地平線に向ける（水準器アプリなどで垂直に立てる）。仰角が 0 ± 2° 程度になる。
3. **真上**：画面を下にして、カメラを真上に向ける。仰角が 88〜90 になる。そのまま水平に回しても仰角は変わらず、値が飛ばない。
4. **真下**：画面を上にして机に置く。仰角が -90 付近。
5. **ロール**：縦持ちで地平線に向けたまま、時計回りに傾けるとロールが正、反時計回りで負。横持ち（右に 90° 回す）で約 +90。
6. **方位（市販のコンパスアプリと比較）**：Google マップのコンパスや、真北・磁北を切り替えられるコンパスアプリと比べる。
   - そのアプリが「真北」表示なら、このアプリの方位角（真北）と比べる。「磁北」表示なら、磁北の方位角と比べる。差が 5° 以内か。
   - 東西南北の4方向で比べる（特定の方向だけずれるなら、地磁気の乱れかキャリブレーション不足）。
   - コンパスアプリは端末の上端の方向を出すことが多い。端末を水平に持って比べるか、このアプリは縦に立ててカメラの方向で比べる。
7. **方位（地図で確認）**：地図で方向のわかる目標（遠くのビル、道路のまっすぐな区間）を照準に入れて、地図から読んだ方位と比べる。いちばん信頼できる確認。
8. **見上げたときの方位**：カメラを北に向けたまま、30°、60°、85° と見上げていく。方位角が北（0 付近）のまま変わらない（端末の上端の方向を使う方式だと、ここで 180° ずれる）。
9. **偏角**：東京付近なら約 -8°（地域で -5〜-10°）。国土地理院の地磁気値（偏角一覧図）と比べて 1° 以内か。
10. **地磁気の強さ**：実測と WMM の全磁力（日本で約 45〜50 µT）が近い。PC やスピーカーに近づけると実測が大きく変わり、方位も狂うことを確かめる（乱れの目安として使えるか）。
11. **精度**：8の字に振る前と後で、方位の精度（`accuracy`）の表示が変わるか。変わらないなら、この値は当てにならないとメモする。
12. **位置**：緯度経度が地図上の現在地と合う。水平精度が屋外で 5〜20m 程度。高度は海抜より 30〜40m ほど大きくても正常（楕円体高のため）。
13. **記録**：「記録」を押すと JSON が表示される。端末を静止させて押すと `stddev_deg` が 0.5 未満、振りながら押すと大きくなる。
14. **再現性**：同じ場所・同じ目標で5回記録し、方位角のばらつきをメモする。

結果（機種名、Android のバージョン、各項目のずれ）は、この文書の末尾か Issue に書き残す。方位のずれが大きい・不安定なら、ネイティブのモジュールを検討する。
