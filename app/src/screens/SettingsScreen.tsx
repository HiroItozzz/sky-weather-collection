// 設定・送信状況の画面。仕様は docs/m3-app-capture.md の 7.2 節。
import { useCallback, useEffect, useState } from "react";
import {
  Platform,
  Pressable,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { randomUUID } from "expo-crypto";
import { observationStore } from "../observationStore";
import { loadSettings, normalizeServerUrl, saveSettings } from "../settings";
import type { QueueItem, QueueState, UploadQueue } from "../uploadQueue";
import { warnOnFailure } from "../useUploadQueue";

const CHECK_TIMEOUT_MS = 10_000;
const URL_FORMAT_MESSAGE =
  "サーバーの URL は、http://192.168.0.10:8000 のように、http:// か https:// で始めて、パスを付けずに入れてください。";

type Props = {
  queue: UploadQueue;
  queueState: QueueState;
  onBack: () => void;
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
  const health = await getStatus(`${serverUrl}/healthz`);
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

export default function SettingsScreen({ queue, queueState, onBack }: Props) {
  const [serverUrl, setServerUrl] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [formMessage, setFormMessage] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [items, setItems] = useState<QueueItem[]>([]);

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

  const onSave = async () => {
    const normalized = normalizeServerUrl(serverUrl);
    if (normalized === null) {
      setFormMessage(URL_FORMAT_MESSAGE);
      return;
    }
    const code = inviteCode.trim();
    try {
      await saveSettings({ serverUrl: normalized, inviteCode: code });
    } catch (e) {
      console.warn("設定の保存に失敗しました", e);
      setFormMessage("設定を保存できませんでした。");
      return;
    }
    setServerUrl(normalized);
    setInviteCode(code);
    setFormMessage("保存しました。");
    warnOnFailure(queue.resumeAfterSettingsSaved(), "設定の保存後の送信");
  };

  const onCheck = async () => {
    const normalized = normalizeServerUrl(serverUrl);
    if (normalized === null) {
      setFormMessage(URL_FORMAT_MESSAGE);
      return;
    }
    setChecking(true);
    setFormMessage("確かめています…");
    try {
      setFormMessage(await checkConnection(normalized, inviteCode.trim()));
    } finally {
      setChecking(false);
    }
  };

  const onRetry = (id: string) => {
    warnOnFailure(
      queue
        .retryRejected(id)
        .then(() => reloadItems())
        .then(() => queue.runNow()),
      "送り直し",
    );
  };

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Pressable onPress={onBack} style={styles.smallButton}>
          <Text style={styles.buttonText}>撮影に戻る</Text>
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
  disabled: { backgroundColor: "#90a4ae" },
  buttonText: { color: "#fff", fontSize: 15, fontWeight: "bold" },
});
