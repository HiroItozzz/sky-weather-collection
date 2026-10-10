// センサー（DeviceMotion・Magnetometer・位置・heading）の購読をまとめたフック。仕様は docs/m2-app-sensors.md の 6 節。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Location from "expo-location";
import { DeviceMotion, Magnetometer } from "expo-sensors";
import { cameraAngles, rotationFromDeviceMotion } from "./orientation";
import { magneticModel } from "./declination";
import type { LocationInput } from "./record";
import { addSample, EMPTY_BUFFER, extractWindow, type SampleBuffer } from "./sampleClock";

/** サンプルを持っておく長さ（ミリ秒）。撮影に数秒かかっても、押した 0.5 秒前からの分が残るようにする */
const KEEP_MS = 10_000;
/** 表示用の状態を更新する間隔（ミリ秒）。約 10 回/秒 */
const DISPLAY_INTERVAL_MS = 100;

export type SensorView = {
  /** 磁北基準の方位角（度）。DeviceMotion がまだ届いていなければ null */
  magneticAzimuthDeg: number | null;
  pitchDeg: number | null;
  rollDeg: number | null;
  /** 地磁気センサーの強さ（µT） */
  magnetometerUT: number | null;
  headingAccuracy: number | null;
  location: LocationInput | null;
  /** 位置を測ってからの経過秒数 */
  locationAgeSec: number | null;
};

const EMPTY_VIEW: SensorView = {
  magneticAzimuthDeg: null,
  pitchDeg: null,
  rollDeg: null,
  magnetometerUT: null,
  headingAccuracy: null,
  location: null,
  locationAgeSec: null,
};

export function useSensors() {
  const samplesRef = useRef<SampleBuffer>(EMPTY_BUFFER);
  const magnetometerRef = useRef<number | null>(null);
  const locationRef = useRef<LocationInput | null>(null);
  const headingAccuracyRef = useRef<number | null>(null);

  const [view, setView] = useState<SensorView>(EMPTY_VIEW);
  const [motionAvailable, setMotionAvailable] = useState<boolean | null>(null);
  const [locationGranted, setLocationGranted] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    const subscriptions: { remove: () => void }[] = [];

    // 後から解除される場合に備えて、登録が終わった時点で cancelled を見る
    const keep = (sub: { remove: () => void }) => {
      if (cancelled) sub.remove();
      else subscriptions.push(sub);
    };

    DeviceMotion.isAvailableAsync().then((ok) => {
      if (cancelled) return;
      setMotionAvailable(ok);
      if (!ok) return;
      DeviceMotion.setUpdateInterval(20);
      keep(
        DeviceMotion.addListener((m) => {
          // rotation は null のことがある
          if (!m.rotation) return;
          const { alpha, beta, gamma, timestamp } = m.rotation;
          // 時刻はセンサーが測った時刻を使う。受け取った時刻は、時計のずれの推定にだけ使う
          const sample = {
            tSensorMs: timestamp * 1000,
            alpha,
            beta,
            gamma,
            R: rotationFromDeviceMotion(alpha, beta, gamma),
          };
          samplesRef.current = addSample(samplesRef.current, sample, Date.now(), KEEP_MS);
        }),
      );
    });

    Magnetometer.setUpdateInterval(100);
    keep(
      Magnetometer.addListener(({ x, y, z }) => {
        magnetometerRef.current = Math.hypot(x, y, z);
      }),
    );

    Location.requestForegroundPermissionsAsync().then(async (perm) => {
      if (cancelled) return;
      setLocationGranted(perm.granted);
      if (!perm.granted) return;
      const position = await Location.watchPositionAsync(
        { accuracy: Location.Accuracy.BestForNavigation, timeInterval: 1000, distanceInterval: 0 },
        (loc) => {
          locationRef.current = loc;
        },
      );
      keep(position);
      const heading = await Location.watchHeadingAsync((h) => {
        headingAccuracyRef.current = h.accuracy;
      });
      keep(heading);
    });

    // 表示用の状態は約 10 回/秒にまとめて更新する
    const timer = setInterval(() => {
      const latest = samplesRef.current.samples[samplesRef.current.samples.length - 1];
      const angles = latest ? cameraAngles(latest.R) : null;
      const location = locationRef.current;
      setView({
        magneticAzimuthDeg: angles?.azimuthDeg ?? null,
        pitchDeg: angles?.pitchDeg ?? null,
        rollDeg: angles?.rollDeg ?? null,
        magnetometerUT: magnetometerRef.current,
        headingAccuracy: headingAccuracyRef.current,
        location,
        locationAgeSec: location ? (Date.now() - location.timestamp) / 1000 : null,
      });
    }, DISPLAY_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
      subscriptions.forEach((s) => s.remove());
    };
  }, []);

  // 偏角と全磁力は、位置が変わったときだけ計算し直す
  const location = view.location;
  const model = useMemo(() => {
    if (!location) return null;
    const { latitude, longitude, altitude } = location.coords;
    return magneticModel(latitude, longitude, altitude, new Date());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location]);

  /** 壁時計の時刻が [startMs, endMs] に入るサンプル（撮影の窓）と、時計のずれ offsetMs を返す。 */
  const getWindow = useCallback((startMs: number, endMs: number) => {
    return extractWindow(samplesRef.current, startMs, endMs);
  }, []);

  /** 記録の時点の最新の位置と heading の accuracy を返す。 */
  const getLatest = useCallback(
    () => ({ location: locationRef.current, headingAccuracy: headingAccuracyRef.current }),
    [],
  );

  return { view, model, motionAvailable, locationGranted, getWindow, getLatest };
}
