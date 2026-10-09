import { useEffect, useState } from "react";
import { BackHandler } from "react-native";
import CaptureScreen from "./src/screens/CaptureScreen";
import SettingsScreen from "./src/screens/SettingsScreen";
import StatsScreen from "./src/screens/StatsScreen";
import TimelineScreen from "./src/screens/TimelineScreen";
import { useUploadQueue } from "./src/useUploadQueue";

type Screen = "capture" | "timeline" | "stats" | "settings";

// Expo Router は使わず、状態で画面を切り替える。キューは画面をまたいで1つだけ持つ。
export default function App() {
  const [screen, setScreen] = useState<Screen>("capture");
  const { queue, state } = useUploadQueue();

  // 撮影画面以外では、Android の戻るボタンで撮影画面に戻る（撮影画面ではふつうに閉じる）
  useEffect(() => {
    if (screen === "capture") return;
    const subscription = BackHandler.addEventListener("hardwareBackPress", () => {
      setScreen("capture");
      return true;
    });
    return () => subscription.remove();
  }, [screen]);

  const backToCapture = () => setScreen("capture");

  switch (screen) {
    case "timeline":
      return <TimelineScreen queueState={state} onBack={backToCapture} />;
    case "stats":
      return <StatsScreen onBack={backToCapture} />;
    case "settings":
      return <SettingsScreen queue={queue} queueState={state} onBack={backToCapture} />;
    default:
      return (
        <CaptureScreen
          queue={queue}
          queueState={state}
          onOpenSettings={() => setScreen("settings")}
          onOpenTimeline={() => setScreen("timeline")}
          onOpenStats={() => setScreen("stats")}
        />
      );
  }
}
