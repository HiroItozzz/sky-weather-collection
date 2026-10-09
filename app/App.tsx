import { useState } from "react";
import CaptureScreen from "./src/screens/CaptureScreen";
import SettingsScreen from "./src/screens/SettingsScreen";
import { useUploadQueue } from "./src/useUploadQueue";

type Screen = "capture" | "settings";

// Expo Router は使わず、状態で画面を切り替える。キューは画面をまたいで1つだけ持つ。
export default function App() {
  const [screen, setScreen] = useState<Screen>("capture");
  const { queue, state } = useUploadQueue();

  return screen === "capture" ? (
    <CaptureScreen queue={queue} queueState={state} onOpenSettings={() => setScreen("settings")} />
  ) : (
    <SettingsScreen queue={queue} queueState={state} onBack={() => setScreen("capture")} />
  );
}
