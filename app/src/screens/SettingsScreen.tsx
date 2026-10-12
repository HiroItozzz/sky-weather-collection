// 設定・送信状況の画面。仕様は docs/m3-app-capture.md の 7.2 節と docs/m6-app-update-invite.md の 1.2・2.3 節。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Platform,
  Pressable,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import Constants from "expo-constants";
import { randomUUID } from "expo-crypto";
import * as Updates from "expo-updates";
import { observationStore } from "../observationStore";
import { loadSettings, normalizeServerUrl, saveSettings } from "../settings";
import { parseSetupLink } from "../setupLink";
import type { QueueItem, QueueState, UploadQueue } from "../uploadQueue";
import { warnOnFailure } from "../useUploadQueue";

const CHECK_TIMEOUT_MS = 10_000;
const URL_FORMAT_MESSAGE = __DEV__
  ? "サーバーの URL は、http://192.168.0.10:8000 のように、http:// か https:// で始めて、パスを付けずに入れてください。"
  : "サーバーの URL は https:// で始めてください。";

type Props = {
  queue: UploadQueue;
  queueState: QueueState;
  onBack: () => void;
  /** 設定リンクでアプリが開かれたときのリンク。受け取ったら onSetupLinkConsumed で空にしてもらう。 */
  setupLink: string | null;
  onSetupLinkConsumed: () => void;
};

/** 接続の確認で GET して、状態コードだけを返す。通信できなければ null。 */
async function getStatus(url: string, token?: string): Promise<number | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CHECK_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      headers: token === undefined ? undefined : { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    });
    return response.status;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function checkConnection(serverUrl: string, inviteCode: string): Promise<string> {
  const health = await getStatus(`${serverUrl}/health`);
  if (health === null) return "サーバーにつながりません。URL と Wi-Fi を確かめてください。";
  if (health !== 200) return `サーバーが正常に答えません（HTTP ${health}）。`;
  const auth = await getStatus(`${serverUrl}/v1/observations/${randomUUID()}`, inviteCode);
  if (auth === 404) return "つながりました。招待コードも使えます。";
  if (auth === 401) return "サーバーにはつながりましたが、招待コードが違います。";
  if (auth === null) return "サーバーにつながりません。URL と Wi-Fi を確かめてください。";
  return `サーバーにはつながりましたが、予想外の答えでした（HTTP ${auth}）。`;
}

const STATE_LABELS: Record<QueueItem["status"]["state"], string> = {
  pending: "未送信",
  sent: "送信済み",
  rejected: "断られた",
};

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("ja-JP");
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** いま動いている版の表示（仕様は docs/m6-app-update-invite.md の 1.2 節）。更新の行は、更新の機能が無効なら null。 */
function versionLines(): { app: string; update: string | null } {
  const version = Constants.expoConfig?.version ?? "不明";
  const versionCode = Constants.expoConfig?.android?.versionCode ?? "不明";
  const app = `アプリ ${version}（${versionCode}）`;
  if (!Updates.isEnabled) return { app, update: null };
  if (Updates.isEmbeddedLaunch || Updates.updateId === null) {
    return { app, update: "更新 なし（APK のまま）" };
  }
  const created = Updates.createdAt;
  const time = created
    ? `${created.getFullYear()}-${pad2(created.getMonth() + 1)}-${pad2(created.getDate())} ` +
      `${pad2(created.getHours())}:${pad2(created.getMinutes())}`
    : "日時不明";
  return { app, update: `更新 ${Updates.updateId.slice(0, 8)}（${time}）` };
}

/** QR コードの読み取り画面。最初に読めた1つだけを onScanned に渡す。 */
function QrScanner({
  onScanned,
  onCancel,
}: {
  onScanned: (text: string) => void;
  onCancel: () => void;
}) {
  const [permission, requestPermission] = useCameraPermissions();
  const handled = useRef(false);

  return (
    <View style={styles.container}>
      {permission?.granted ? (
        <CameraView
          style={StyleSheet.absoluteFill}
          facing="back"
          barcodeScannerSettings={{ barcodeTypes: ["qr"] }}
          onBarcodeScanned={({ data }) => {
            if (handled.current) return;
            handled.current = true;
            onScanned(data);
          }}
        />
      ) : null}
      <View style={styles.header}>
        <Pressable onPress={onCancel} style={styles.smallButton}>
          <Text style={styles.buttonText}>やめる</Text>
        </Pressable>
        {permission?.granted ? (
          <Text style={styles.scanHint}>招待の QR コードを枠に入れてください。</Text>
        ) : permission !== null && permission.canAskAgain ? (
          <Pressable onPress={requestPermission} style={styles.button}>
            <Text style={styles.buttonText}>カメラを許可する</Text>
          </Pressable>
        ) : permission !== null ? (
          <Text style={styles.warn}>
            カメラが拒否されています。端末の設定からカメラを許可してください。
          </Text>
        ) : null}
      </View>
    </View>
  );
}

export default function SettingsScreen({
  queue,
  queueState,
  onBack,
  setupLink,
  onSetupLinkConsumed,
}: Props) {
  const [serverUrl, setServerUrl] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [formMessage, setFormMessage] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [items, setItems] = useState<QueueItem[]>([]);
  const [scanning, setScanning] = useState(false);

  useEffect(() => {
    loadSettings()
      .then((s) => {
        setServerUrl(s.serverUrl);
        setInviteCode(s.inviteCode);
      })
      .catch((e: unknown) => {
        console.warn("設定の読み込みに失敗しました", e);
      });
  }, []);

  const reloadItems = useCallback(async () => {
    try {
      const list = await observationStore.list();
      list.sort((a, b) => Date.parse(b.capturedAt) - Date.parse(a.capturedAt));
      setItems(list);
    } catch (e) {
      console.warn("送信状況の読み込みに失敗しました", e);
    }
  }, []);

  // 件数が変わったときと、送信の周が終わったときに一覧を読み直す
  useEffect(() => {
    void reloadItems();
  }, [reloadItems, queueState.pendingCount, queueState.running]);

  /** 保存する。手入力の「保存」と、設定リンク・QR コードの「設定する」で共通。保存できたら正規化した URL を返す。 */
  const saveInput = async (url: string, code: string): Promise<string | null> => {
    const normalized = normalizeServerUrl(url, { allowHttp: __DEV__ });
    if (normalized === null) {
      setFormMessage(URL_FORMAT_MESSAGE);
      return null;
    }
    const trimmedCode = code.trim();
    try {
      await saveSettings({ serverUrl: normalized, inviteCode: trimmedCode });
    } catch (e) {
      console.warn("設定の保存に失敗しました", e);
      setFormMessage("設定を保存できませんでした。");
      return null;
    }
    setServerUrl(normalized);
    setInviteCode(trimmedCode);
    setFormMessage("保存しました。");
    warnOnFailure(queue.resumeAfterSettingsSaved(), "設定の保存後の送信");
    return normalized;
  };

  /** 接続を確かめて、結果を表示する。手入力の「接続を確かめる」と、設定リンク・QR コードで共通。 */
  const checkInput = async (normalizedUrl: string, code: string) => {
    setChecking(true);
    setFormMessage("確かめています…");
    try {
      setFormMessage(await checkConnection(normalizedUrl, code.trim()));
    } finally {
      setChecking(false);
    }
  };

  const onSave = async () => {
    await saveInput(serverUrl, inviteCode);
  };

  const onCheck = async () => {
    const normalized = normalizeServerUrl(serverUrl, { allowHttp: __DEV__ });
    if (normalized === null) {
      setFormMessage(URL_FORMAT_MESSAGE);
      return;
    }
    await checkInput(normalized, inviteCode);
  };

  // 読み取った QR コードや、開かれた設定リンクは、確認のダイアログを出してから保存する
  const confirmSetupLink = (text: string) => {
    setScanning(false);
    const parsed = parseSetupLink(text);
    if ("error" in parsed) {
      setFormMessage(parsed.error);
      return;
    }
    Alert.alert(
      "設定の確認",
      `このサーバーに写真と位置を送るように設定します。\n${parsed.serverUrl}\n心当たりのない QR コードなら「やめる」を押してください。`,
      [
        { text: "やめる", style: "cancel" },
        {
          text: "設定する",
          onPress: () => {
            void (async () => {
              const normalized = await saveInput(parsed.serverUrl, parsed.inviteCode);
              if (normalized !== null) await checkInput(normalized, parsed.inviteCode);
            })();
          },
        },
      ],
    );
  };

  useEffect(() => {
    if (setupLink === null) return;
    onSetupLinkConsumed();
    confirmSetupLink(setupLink);
    // setupLink が変わったときだけ処理する
  }, [setupLink]);

  const onRetry = (id: string) => {
    warnOnFailure(
      queue
        .retryRejected(id)
        .then(() => reloadItems())
        .then(() => queue.runNow()),
      "送り直し",
    );
  };

  if (scanning) {
    return <QrScanner onScanned={confirmSetupLink} onCancel={() => setScanning(false)} />;
  }

  const version = versionLines();

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Pressable onPress={onBack} style={styles.smallButton}>
          <Text style={styles.buttonText}>← 撮影に戻る</Text>
        </Pressable>
      </View>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text style={styles.heading}>サーバー</Text>
        <TextInput
          style={styles.input}
          value={serverUrl}
          onChangeText={setServerUrl}
          placeholder="http://192.168.0.10:8000"
          placeholderTextColor="#888"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
        />
        <TextInput
          style={styles.input}
          value={inviteCode}
          onChangeText={setInviteCode}
          placeholder="招待コード"
          placeholderTextColor="#888"
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
        />
        <Pressable onPress={() => setScanning(true)} style={styles.button}>
          <Text style={styles.buttonText}>QRコードで設定</Text>
        </Pressable>
        <View style={styles.row}>
          <Pressable onPress={onSave} style={styles.button}>
            <Text style={styles.buttonText}>保存</Text>
          </Pressable>
          <Pressable
            onPress={onCheck}
            disabled={checking}
            style={[styles.button, checking && styles.disabled]}
          >
            <Text style={styles.buttonText}>接続を確かめる</Text>
          </Pressable>
        </View>
        {formMessage !== null ? <Text style={styles.message}>{formMessage}</Text> : null}
        {queueState.blockedReason === "auth" ? (
          <Text style={styles.warn}>
            招待コードが違うため、送信を止めています。確かめて保存し直してください。
          </Text>
        ) : null}
        {queueState.blockedReason === "config" ? (
          <Text style={styles.warn}>
            サーバーに断られたため、送信を止めています。サーバーの URL を確かめて保存し直してください。
          </Text>
        ) : null}

        <Text style={styles.heading}>送信状況</Text>
        <View style={styles.row}>
          <Text style={styles.text}>未送信 {queueState.pendingCount} 件</Text>
          <Pressable
            onPress={() => warnOnFailure(queue.runNow(), "今すぐ送る")}
            style={styles.button}
          >
            <Text style={styles.buttonText}>今すぐ送る</Text>
          </Pressable>
        </View>
        {items.length === 0 ? <Text style={styles.text}>まだ撮影した写真はありません。</Text> : null}
        {items.map((item) => (
          <View key={item.id} style={styles.item}>
            <Text style={styles.text}>
              {formatTime(item.capturedAt)}　{STATE_LABELS[item.status.state]}
            </Text>
            {item.status.attempts > 0 ? (
              <Text style={styles.sub}>失敗 {item.status.attempts} 回</Text>
            ) : null}
            {item.status.last_error ? (
              <Text style={styles.sub}>最後のエラー: {item.status.last_error}</Text>
            ) : null}
            {item.status.state === "rejected" ? (
              <Pressable onPress={() => onRetry(item.id)} style={styles.button}>
                <Text style={styles.buttonText}>送り直す</Text>
              </Pressable>
            ) : null}
          </View>
        ))}

        <Text style={styles.version}>{version.app}</Text>
        {version.update !== null ? <Text style={styles.version}>{version.update}</Text> : null}
      </ScrollView>
    </View>
  );
}

// ステータスバーの分の余白（SafeAreaView を使わずパディングで済ませる）
const TOP_PADDING = (Platform.OS === "android" ? (StatusBar.currentHeight ?? 24) : 48) + 8;

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#fff" },
  header: { paddingTop: TOP_PADDING, paddingHorizontal: 12, paddingBottom: 8 },
  content: { padding: 12, paddingBottom: 48 },
  heading: { fontSize: 18, fontWeight: "bold", marginTop: 16, marginBottom: 8 },
  input: {
    borderWidth: 1,
    borderColor: "#999",
    borderRadius: 6,
    paddingHorizontal: 10,
    paddingVertical: 8,
    marginBottom: 8,
    fontSize: 16,
    color: "#000", // 端末がダークモードでも背景（白）に合う文字色にする
  },
  row: { flexDirection: "row", alignItems: "center", gap: 12, marginVertical: 4 },
  text: { fontSize: 15 },
  sub: { fontSize: 13, color: "#555", marginTop: 2 },
  message: { fontSize: 14, marginTop: 8 },
  warn: { fontSize: 14, color: "#b26a00", marginTop: 8 },
  item: { borderTopWidth: 1, borderTopColor: "#ddd", paddingVertical: 8 },
  button: {
    alignSelf: "flex-start",
    marginTop: 4,
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 20,
    backgroundColor: "#1976d2",
  },
  smallButton: {
    alignSelf: "flex-start",
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 20,
    backgroundColor: "#1976d2",
  },
  version: { fontSize: 12, color: "#777", marginTop: 4 },
  scanHint: { fontSize: 15, color: "#fff", marginTop: 12 },
  disabled: { backgroundColor: "#90a4ae" },
  buttonText: { color: "#fff", fontSize: 15, fontWeight: "bold" },
});
