// 撮影の画面（起動したときの画面）。仕様は docs/m3-app-capture.md の 7.1 節。
import { useEffect, useRef, useState } from "react";
import { Platform, Pressable, StatusBar, StyleSheet, Text, View } from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import Constants from "expo-constants";
import { randomUUID } from "expo-crypto";
import * as Device from "expo-device";
import { createApiClient } from "../api";
import { weatherSummary } from "../format";
import type { UserGuess } from "../metadata";
import { toTrueAzimuth } from "../orientation";
import { pollWeather } from "../pollWeather";
import type { Sample } from "../record";
import { saveCapture } from "../observationStore";
import { loadSettings, type Settings } from "../settings";
import type { QueueState, UploadQueue } from "../uploadQueue";
import { warnOnFailure } from "../useUploadQueue";
import { useSensors } from "../useSensors";

/** シャッターを押せる仰角の下限（度）。設計メモ 6 節 */
export const MIN_ELEVATION_DEG = 20;
/** 押した時刻の後ろのサンプルがそろうまで待つ時間（ミリ秒） */
const SAMPLE_WAIT_MS = 500;
/** この秒数より古い位置は警告を出す */
const STALE_LOCATION_SEC = 60;
/** 地磁気の実測が WMM の全磁力からこの割合以上ずれたら、キャリブレーションを促す */
const MAGNETIC_DEVIATION_RATIO = 0.2;
const SAVED_MESSAGE_MS = 2000;
/** 撮影時の天気を問い合わせる間隔と、あきらめるまでの時間（ミリ秒） */
const WEATHER_POLL_INTERVAL_MS = 5000;
const WEATHER_POLL_TIMEOUT_MS = 60_000;

const GUESS_OPTIONS: { value: UserGuess | null; label: string }[] = [
  { value: "rain", label: "降る" },
  { value: "no_rain", label: "降らない" },
  { value: null, label: "わからない" },
];

/** ミリ秒待つ。signal が止まったらすぐ戻る。 */
function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

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

type Props = {
  queue: UploadQueue;
  queueState: QueueState;
  onOpenSettings: () => void;
  onOpenTimeline: () => void;
  onOpenStats: () => void;
};

export default function CaptureScreen({
  queue,
  queueState,
  onOpenSettings,
  onOpenTimeline,
  onOpenStats,
}: Props) {
  const [cameraPermission, requestCameraPermission] = useCameraPermissions();
  const { view, model, motionAvailable, locationGranted, getSamplesAround, getLatest } =
    useSensors();
  const cameraRef = useRef<CameraView>(null);
  const [cameraReady, setCameraReady] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [showDetails, setShowDetails] = useState(false);
  // 二度押しを同期的にはじくためのフラグ（状態の更新は待たない）
  const shootingRef = useRef(false);
  const [guess, setGuess] = useState<UserGuess | null>(null);
  const [weatherText, setWeatherText] = useState<string | null>(null);
  // 撮影時の天気の問い合わせを止めるためのもの。次のシャッターと画面を離れたときに止める
  const pollAbortRef = useRef<AbortController | null>(null);
  const unmountedRef = useRef(false);
  const api = useRef(createApiClient(fetch)).current;

  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
      pollAbortRef.current?.abort();
    };
  }, []);

  // 設定の画面から戻ったときに読み直すため、この画面を開くたびに読む
  useEffect(() => {
    loadSettings()
      .then(setSettings)
      .catch((e: unknown) => {
        console.warn("設定の読み込みに失敗しました", e);
      });
  }, []);

  useEffect(() => {
    if (message === null) return;
    const timer = setTimeout(() => setMessage(null), SAVED_MESSAGE_MS);
    return () => clearTimeout(timer);
  }, [message]);

  const platformSupported = Platform.OS === "android" || Platform.OS === "ios";
  const declination = model?.declinationDeg ?? null;
  const trueAzimuth =
    view.magneticAzimuthDeg !== null && declination !== null
      ? toTrueAzimuth(view.magneticAzimuthDeg, declination)
      : null;
  const coords = view.location?.coords;
  const accuracyName =
    view.headingAccuracy === null ? "--" : (ACCURACY_NAMES[view.headingAccuracy] ?? "--");

  const cameraGranted = cameraPermission?.granted === true;
  const cameraDenied = cameraPermission !== null && !cameraPermission.granted;
  const elevationOk = view.pitchDeg !== null && view.pitchDeg >= MIN_ELEVATION_DEG;
  const canShoot = platformSupported && cameraGranted && cameraReady && elevationOk && !saving;

  const needsCalibration =
    view.headingAccuracy === 0 ||
    view.headingAccuracy === 1 ||
    (view.magnetometerUT !== null &&
      model !== null &&
      Math.abs(view.magnetometerUT - model.totalIntensityUT) >=
        model.totalIntensityUT * MAGNETIC_DEVIATION_RATIO);

  const locationAccuracyMissing =
    coords !== undefined && (coords.accuracy === null || coords.accuracy <= 0);
  const locationStale = view.locationAgeSec !== null && view.locationAgeSec >= STALE_LOCATION_SEC;

  /** 保存した観測の撮影時の天気を、取れるまで問い合わせて表示する（5 節）。 */
  const showWeatherWhenReady = async (observationId: string) => {
    if (unmountedRef.current) return;
    const current = await loadSettings();
    if (current.serverUrl === "" || current.inviteCode === "") return;
    const controller = new AbortController();
    // 保存の最中に次のシャッターが押されることはない（saving の間は押せない）
    pollAbortRef.current = controller;
    const weather = await pollWeather({
      get: async () => api.getObservation(await loadSettings(), observationId),
      sleep: abortableSleep,
      now: Date.now,
      intervalMs: WEATHER_POLL_INTERVAL_MS,
      timeoutMs: WEATHER_POLL_TIMEOUT_MS,
      signal: controller.signal,
    });
    if (weather !== null && !controller.signal.aborted) {
      setWeatherText(`撮影時の天気：${weatherSummary(weather)}`);
    }
  };

  const onShutter = async () => {
    const camera = cameraRef.current;
    if (shootingRef.current || camera === null || !canShoot) return;
    shootingRef.current = true;
    const pressedAtMs = Date.now();
    // 予想は押した時点で固定し、次の1枚に持ち越さないように「わからない」へ戻す
    const userGuess = guess;
    setGuess(null);
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
    setWeatherText(null);
    setSaving(true);
    setMessage(null);
    try {
      // 押した時刻の後ろ 0.5 秒がたったところで、前後 0.5 秒のサンプルを複製しておく。
      // 撮影に時間がかかっても、押した瞬間のサンプルが手元の記録から消えないようにするため。
      const samplesPromise = new Promise<Sample[]>((resolve) => {
        setTimeout(
          () => resolve([...getSamplesAround(pressedAtMs)]),
          Math.max(0, pressedAtMs + SAMPLE_WAIT_MS - Date.now()),
        );
      });
      const picturePromise = camera.takePictureAsync({ skipProcessing: true, exif: true }).then(
        (result) => ({ result, elapsedMs: Date.now() - pressedAtMs }),
      );
      const [{ result: picture, elapsedMs }, samples] = await Promise.all([
        picturePromise,
        samplesPromise,
      ]);

      const { location, headingAccuracy } = getLatest();
      const observationId = randomUUID();
      await saveCapture({
        observationId,
        cachedUri: picture.uri,
        pressedAtMs,
        samples,
        location,
        headingAccuracy,
        exif: picture.exif,
        width: picture.width,
        height: picture.height,
        device: {
          platform: Platform.OS === "ios" ? "ios" : "android",
          os_version: Device.osVersion,
          model: Device.modelName,
          app_version: Constants.expoConfig?.version ?? null,
        },
        userGuess,
      });
      setMessage(`保存しました（撮影 ${elapsedMs} ms）`);
      // 保存の完了後に件数を数え直し、送信を始める
      warnOnFailure(queue.refresh().then(() => queue.runNow()), "撮影後の送信");
      warnOnFailure(showWeatherWhenReady(observationId), "撮影時の天気の取得");
    } catch (e) {
      console.warn("保存に失敗しました", e);
      const reason = e instanceof Error ? e.message : String(e);
      setMessage(`保存に失敗しました（${reason}）`);
    } finally {
      shootingRef.current = false;
      setSaving(false);
    }
  };

  let hint: string | null = null;
  if (!platformSupported) hint = "この端末では撮影できません（Android と iOS だけに対応しています）";
  else if (!elevationOk) hint = `空に向けてください（仰角 ${MIN_ELEVATION_DEG}° 以上）`;

  return (
    <View style={styles.container}>
      {cameraGranted ? (
        <CameraView
          ref={cameraRef}
          style={StyleSheet.absoluteFill}
          facing="back"
          onCameraReady={() => setCameraReady(true)}
        />
      ) : null}

      <View style={styles.crosshair} pointerEvents="none">
        <View style={styles.crossH} />
        <View style={styles.crossV} />
      </View>

      <View style={styles.panel}>
        <View style={styles.navRow}>
          <Pressable onPress={onOpenTimeline} style={styles.navButton}>
            <Text style={styles.buttonText}>タイムライン</Text>
          </Pressable>
          <Pressable onPress={onOpenStats} style={styles.navButton}>
            <Text style={styles.buttonText}>記録</Text>
          </Pressable>
          <Pressable onPress={onOpenSettings} style={styles.navButton}>
            <Text style={styles.buttonText}>設定</Text>
          </Pressable>
        </View>
        <View style={styles.topRow}>
          <View style={styles.values}>
            <Text style={styles.line}>方位角（真北）: {fmt(trueAzimuth, "°")}</Text>
            <Text style={styles.line}>
              仰角: {fmt(view.pitchDeg, "°")}　ロール: {fmt(view.rollDeg, "°")}
            </Text>
            <Text style={styles.line}>
              方位の精度: {accuracyName}　位置の精度: {fmt(coords?.accuracy, " m")}
            </Text>
          </View>
          <View style={styles.actions}>
            <Pressable onPress={onOpenSettings} style={styles.smallButton}>
              <Text style={styles.buttonText}>未送信 {queueState.pendingCount} 件</Text>
            </Pressable>
            <Pressable onPress={() => setShowDetails((v) => !v)} style={styles.smallButton}>
              <Text style={styles.buttonText}>{showDetails ? "詳細を閉じる" : "詳細"}</Text>
            </Pressable>
          </View>
        </View>

        {showDetails ? (
          <View style={styles.details}>
            <Text style={styles.line}>
              磁北: {fmt(view.magneticAzimuthDeg, "°")}　偏角: {fmt(declination, "°")}
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
        ) : null}

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
        {locationGranted !== false && view.location === null ? (
          <Text style={styles.warn}>位置がまだ取れていません。</Text>
        ) : null}
        {locationStale ? (
          <Text style={styles.warn}>位置が古くなっています（{fmt(view.locationAgeSec, " 秒")}前）。</Text>
        ) : null}
        {locationAccuracyMissing ? (
          <Text style={styles.warn}>位置の精度が取れていないため、位置は保存されません。</Text>
        ) : null}
        {motionAvailable === false ? (
          <Text style={styles.warn}>この端末では DeviceMotion が使えません。</Text>
        ) : null}
        {needsCalibration ? (
          <Text style={styles.warn}>方位が不安定です。端末を8の字に振ってください。</Text>
        ) : null}
        {settings !== null && (settings.serverUrl === "" || settings.inviteCode === "") ? (
          <Text style={styles.warn}>
            サーバーが設定されていません。右上のボタンから設定してください。
          </Text>
        ) : null}
        {queueState.blockedReason === "auth" ? (
          <Text style={styles.warn}>招待コードが違います。設定で確かめてください。</Text>
        ) : null}
        {queueState.blockedReason === "config" ? (
          <Text style={styles.warn}>
            サーバーに断られました。設定でサーバーの URL を確かめてください。
          </Text>
        ) : null}
      </View>

      <View style={styles.bottom} pointerEvents="box-none">
        {message !== null ? <Text style={styles.message}>{message}</Text> : null}
        {weatherText !== null ? <Text style={styles.message}>{weatherText}</Text> : null}
        {hint !== null ? <Text style={styles.message}>{hint}</Text> : null}
        <Text style={styles.message}>1時間後に、ここで雨が降ると思いますか？</Text>
        <View style={styles.guessRow}>
          {GUESS_OPTIONS.map((option) => (
            <Pressable
              key={option.label}
              onPress={() => setGuess(option.value)}
              style={[styles.guessButton, guess === option.value && styles.guessSelected]}
            >
              <Text style={styles.buttonText}>{option.label}</Text>
            </Pressable>
          ))}
        </View>
        <Pressable
          onPress={onShutter}
          disabled={!canShoot}
          style={[styles.shutter, !canShoot && styles.disabled]}
        >
          <Text style={styles.buttonText}>{saving ? "保存中…" : "撮影"}</Text>
        </Pressable>
      </View>
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
  navRow: { flexDirection: "row", gap: 8, marginBottom: 6 },
  navButton: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 20,
    backgroundColor: "#1976d2",
  },
  guessRow: { flexDirection: "row", gap: 8, marginBottom: 12 },
  guessButton: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 20,
    backgroundColor: "rgba(0,0,0,0.6)",
    borderWidth: 2,
    borderColor: "#607d8b",
  },
  guessSelected: { backgroundColor: "#1976d2", borderColor: "#fff" },
  topRow: { flexDirection: "row", justifyContent: "space-between" },
  values: { flexShrink: 1 },
  actions: { alignItems: "flex-end" },
  details: { marginTop: 6 },
  line: { color: "#fff", fontSize: 14, marginVertical: 1 },
  warn: { color: "#ffd54f", fontSize: 14, marginVertical: 2 },
  bottom: { position: "absolute", bottom: 48, left: 0, right: 0, alignItems: "center" },
  message: {
    color: "#fff",
    fontSize: 14,
    marginBottom: 8,
    paddingHorizontal: 12,
    paddingVertical: 4,
    borderRadius: 8,
    backgroundColor: "rgba(0,0,0,0.6)",
  },
  shutter: {
    paddingHorizontal: 48,
    paddingVertical: 16,
    borderRadius: 32,
    backgroundColor: "#1976d2",
  },
  smallButton: {
    alignSelf: "flex-end",
    marginTop: 4,
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 20,
    backgroundColor: "#1976d2",
  },
  disabled: { backgroundColor: "#607d8b" },
  buttonText: { color: "#fff", fontSize: 15, fontWeight: "bold" },
});
