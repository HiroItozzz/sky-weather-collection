import { useState } from "react";
import { Platform, Pressable, ScrollView, StatusBar, StyleSheet, Text, View } from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import { buildRecord, type SensorRecord } from "./src/record";
import { toTrueAzimuth } from "./src/orientation";
import { useSensors } from "./src/useSensors";

/** 小数第1位まで。値がないときは「--」 */
function fmt(value: number | null | undefined, unit = ""): string {
  return value === null || value === undefined ? "--" : `${value.toFixed(1)}${unit}`;
}

const ACCURACY_NAMES: Record<number, string> = {
  3: "high",
  2: "medium",
  1: "low",
  0: "unreliable",
};

export default function App() {
  const [cameraPermission, requestCameraPermission] = useCameraPermissions();
  const { view, model, motionAvailable, locationGranted, getSamplesAround, getLatest } =
    useSensors();
  const [waiting, setWaiting] = useState(false);
  const [recordJson, setRecordJson] = useState<string | null>(null);

  const declination = model?.declinationDeg ?? null;
  const trueAzimuth =
    view.magneticAzimuthDeg !== null && declination !== null
      ? toTrueAzimuth(view.magneticAzimuthDeg, declination)
      : null;
  const coords = view.location?.coords;
  const accuracyName =
    view.headingAccuracy === null ? "--" : (ACCURACY_NAMES[view.headingAccuracy] ?? "--");

  const onRecord = () => {
    const pressedAtMs = Date.now();
    setWaiting(true);
    // 押した時刻の後ろ 0.5 秒のサンプルがそろうまで待つ
    setTimeout(() => {
      const { location, headingAccuracy } = getLatest();
      const record: SensorRecord = buildRecord({
        pressedAtMs,
        samples: getSamplesAround(pressedAtMs),
        location,
        headingAccuracy,
      });
      setRecordJson(JSON.stringify(record, null, 2));
      setWaiting(false);
    }, 500);
  };

  const cameraDenied = cameraPermission !== null && !cameraPermission.granted;

  return (
    <View style={styles.container}>
      {cameraPermission?.granted ? (
        <CameraView style={StyleSheet.absoluteFill} facing="back" />
      ) : null}

      <View style={styles.crosshair} pointerEvents="none">
        <View style={styles.crossH} />
        <View style={styles.crossV} />
      </View>

      <View style={styles.panel}>
        {cameraDenied ? (
          <Text style={styles.warn}>
            {cameraPermission.canAskAgain
              ? "カメラの許可がありません。"
              : "カメラが拒否されています。端末の設定からカメラを許可してください。"}
          </Text>
        ) : null}
        {cameraDenied && cameraPermission.canAskAgain ? (
          <Pressable onPress={requestCameraPermission} style={styles.smallButton}>
            <Text style={styles.buttonText}>カメラを許可する</Text>
          </Pressable>
        ) : null}
        {locationGranted === false ? (
          <Text style={styles.warn}>
            位置の許可が拒否されています。端末の設定から位置情報を許可してください。
          </Text>
        ) : null}
        {motionAvailable === false ? (
          <Text style={styles.warn}>この端末では DeviceMotion が使えません。</Text>
        ) : null}

        <Text style={styles.line}>
          方位角（真北）: {fmt(trueAzimuth, "°")}　磁北: {fmt(view.magneticAzimuthDeg, "°")}
        </Text>
        <Text style={styles.line}>
          仰角: {fmt(view.pitchDeg, "°")}　ロール: {fmt(view.rollDeg, "°")}
        </Text>
        <Text style={styles.line}>
          方位の精度: {accuracyName}　偏角: {fmt(declination, "°")}
        </Text>
        <Text style={styles.line}>
          地磁気 実測: {fmt(view.magnetometerUT, " µT")}　WMM: {fmt(model?.totalIntensityUT, " µT")}
        </Text>
        <Text style={styles.line}>
          緯度: {coords ? coords.latitude.toFixed(6) : "--"}　経度:{" "}
          {coords ? coords.longitude.toFixed(6) : "--"}
        </Text>
        <Text style={styles.line}>
          水平精度: {fmt(coords?.accuracy, " m")}　高度: {fmt(coords?.altitude, " m")}
        </Text>
        <Text style={styles.line}>位置の経過: {fmt(view.locationAgeSec, " 秒")}</Text>
      </View>

      <Pressable
        onPress={onRecord}
        disabled={waiting}
        style={[styles.recordButton, waiting && styles.disabled]}
      >
        <Text style={styles.buttonText}>{waiting ? "記録中…" : "記録"}</Text>
      </Pressable>

      {recordJson !== null ? (
        <View style={styles.overlay}>
          <ScrollView style={styles.jsonScroll}>
            <Text style={styles.json} selectable>
              {recordJson}
            </Text>
          </ScrollView>
          <Pressable onPress={() => setRecordJson(null)} style={styles.smallButton}>
            <Text style={styles.buttonText}>閉じる</Text>
          </Pressable>
        </View>
      ) : null}
    </View>
  );
}

// ステータスバーの分の余白（SafeAreaView を使わずパディングで済ませる）
const TOP_PADDING = (Platform.OS === "android" ? (StatusBar.currentHeight ?? 24) : 48) + 8;

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#000" },
  crosshair: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center" },
  crossH: { position: "absolute", width: 40, height: 2, backgroundColor: "#fff" },
  crossV: { position: "absolute", width: 2, height: 40, backgroundColor: "#fff" },
  panel: {
    position: "absolute",
    top: TOP_PADDING,
    left: 8,
    right: 8,
    padding: 10,
    borderRadius: 8,
    backgroundColor: "rgba(0,0,0,0.55)",
  },
  line: { color: "#fff", fontSize: 14, marginVertical: 1 },
  warn: { color: "#ffd54f", fontSize: 14, marginVertical: 2 },
  recordButton: {
    position: "absolute",
    bottom: 48,
    alignSelf: "center",
    paddingHorizontal: 48,
    paddingVertical: 16,
    borderRadius: 32,
    backgroundColor: "#1976d2",
  },
  smallButton: {
    alignSelf: "center",
    marginTop: 8,
    paddingHorizontal: 24,
    paddingVertical: 10,
    borderRadius: 20,
    backgroundColor: "#1976d2",
  },
  disabled: { backgroundColor: "#607d8b" },
  buttonText: { color: "#fff", fontSize: 16, fontWeight: "bold" },
  overlay: {
    position: "absolute",
    top: TOP_PADDING,
    left: 8,
    right: 8,
    bottom: 120,
    padding: 12,
    borderRadius: 8,
    backgroundColor: "rgba(0,0,0,0.85)",
  },
  jsonScroll: { flex: 1 },
  json: { color: "#b2ff59", fontFamily: Platform.OS === "android" ? "monospace" : "Menlo", fontSize: 12 },
});
