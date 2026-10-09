// 記録の画面。仕様は docs/m5-app.md の 6.4 節と 6.5 節。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { createApiClient, type Stats } from "../api";
import { loadStatsCache, saveStatsCache } from "../cache";
import { categoryLabel, hitRateLabel, isCloudyRainScarce, type CategoryKey } from "../format";
import { loadSettings } from "../settings";

const OFFLINE_MESSAGE = "通信できないため、前回取得した内容を表示しています";
const CATEGORIES: CategoryKey[] = ["clear", "cloudy", "rain", "unknown"];

type Props = {
  onBack: () => void;
};

export default function StatsScreen({ onBack }: Props) {
  const api = useRef(createApiClient(fetch)).current;
  const [stats, setStats] = useState<Stats | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const reload = useCallback(async () => {
    const settings = await loadSettings();
    if (settings.serverUrl === "" || settings.inviteCode === "") {
      setNotice("設定の画面でサーバーを設定してください");
      return;
    }
    const result = await api.getStats(settings);
    if (result.kind === "ok") {
      setStats(result.data);
      setNotice(null);
      saveStatsCache(result.data);
      return;
    }
    if (result.kind === "auth") {
      setNotice("招待コードを確かめてください");
      return;
    }
    const cached = await loadStatsCache();
    if (cached !== null) {
      setStats(cached);
      setNotice(OFFLINE_MESSAGE);
    } else {
      setNotice(
        result.kind === "error"
          ? `記録を取得できませんでした（${result.error}）`
          : "記録を取得できませんでした（サーバーに見つかりません）",
      );
    }
  }, [api]);

  useEffect(() => {
    reload().catch((e: unknown) => {
      console.warn("記録の取得に失敗しました", e);
    });
  }, [reload]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await reload();
    } catch (e) {
      console.warn("記録の取得に失敗しました", e);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Pressable onPress={onBack} style={styles.smallButton}>
          <Text style={styles.buttonText}>← 撮影に戻る</Text>
        </Pressable>
      </View>
      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
      >
        {notice !== null ? <Text style={styles.warn}>{notice}</Text> : null}
        {stats !== null ? (
          <View>
            <Text style={styles.text}>送った枚数：{stats.observations_total} 枚</Text>
            <Text style={styles.text}>答え合わせが済んだ枚数：{stats.answered_total} 枚</Text>
            <Text style={styles.text}>予想の的中率：{hitRateLabel(stats)}</Text>

            <Text style={styles.heading}>天気ごとの枚数</Text>
            {CATEGORIES.map((key) => (
              <Text key={key} style={styles.text}>
                {categoryLabel(key)}：{stats.by_category[key]} 枚
              </Text>
            ))}
            {isCloudyRainScarce(stats.by_category) ? (
              <Text style={styles.encourage}>曇りや雨の日の写真は特に貴重です</Text>
            ) : null}
          </View>
        ) : null}
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
  heading: { fontSize: 18, fontWeight: "bold", marginTop: 20, marginBottom: 8 },
  text: { fontSize: 16, marginVertical: 3 },
  warn: { fontSize: 14, color: "#b26a00", marginBottom: 12 },
  encourage: { fontSize: 15, color: "#1976d2", fontWeight: "bold", marginTop: 12 },
  smallButton: {
    alignSelf: "flex-start",
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 20,
    backgroundColor: "#1976d2",
  },
  buttonText: { color: "#fff", fontSize: 15, fontWeight: "bold" },
});
