// タイムラインの画面。仕様は docs/m5-app.md の 6.3 節と 6.5 節。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  FlatList,
  Image,
  Platform,
  Pressable,
  RefreshControl,
  StatusBar,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { createApiClient, type ObservationView } from "../api";
import { loadTimelineCache, saveTimelineCache } from "../cache";
import {
  answerLabel,
  correctLabel,
  formatDateTime,
  guessLabel,
  weatherSummary,
} from "../format";
import { loadSettings } from "../settings";
import { thumbnailUri } from "../thumbnail";
import type { QueueState } from "../uploadQueue";

const PAGE_SIZE = 50;
const OFFLINE_MESSAGE = "通信できないため、前回取得した内容を表示しています";

type Props = {
  queueState: QueueState;
  onBack: () => void;
};

function Item({ item }: { item: ObservationView }) {
  const uri = thumbnailUri(item.observation_id);
  const correct = correctLabel(item.correct);
  return (
    <View style={styles.item}>
      {uri !== null ? (
        <Image source={{ uri }} style={styles.thumb} resizeMode="cover" />
      ) : (
        <View style={[styles.thumb, styles.thumbEmpty]}>
          <Text style={styles.sub}>画像なし</Text>
        </View>
      )}
      <View style={styles.itemBody}>
        <Text style={styles.text}>{formatDateTime(item.captured_at)}</Text>
        <Text style={styles.sub}>予想：{guessLabel(item.user_guess)}</Text>
        <Text style={styles.sub}>
          答え：{answerLabel(item.answer.result, item.answer.pending)}
          {correct !== null ? `　${correct}` : ""}
        </Text>
        {item.weather_at_capture !== null ? (
          <Text style={styles.sub}>撮影時の天気：{weatherSummary(item.weather_at_capture)}</Text>
        ) : null}
      </View>
    </View>
  );
}

export default function TimelineScreen({ queueState, onBack }: Props) {
  const api = useRef(createApiClient(fetch)).current;
  const [items, setItems] = useState<ObservationView[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  /** 最初のページを取り直す。失敗したら保存した内容を出す。 */
  const reload = useCallback(async () => {
    const settings = await loadSettings();
    if (settings.serverUrl === "" || settings.inviteCode === "") {
      setNotice("設定の画面でサーバーを設定してください");
      return;
    }
    const result = await api.listObservations(settings, { limit: PAGE_SIZE });
    if (result.kind === "ok") {
      setItems(result.data.observations);
      setNextBefore(result.data.next_before);
      setNotice(null);
      saveTimelineCache(result.data);
      return;
    }
    if (result.kind === "auth") {
      setNotice("招待コードを確かめてください");
      return;
    }
    const cached = await loadTimelineCache();
    if (cached !== null) {
      setItems(cached.observations);
      setNextBefore(cached.next_before);
      setNotice(OFFLINE_MESSAGE);
    } else {
      setNotice(
        result.kind === "error"
          ? `一覧を取得できませんでした（${result.error}）`
          : "一覧を取得できませんでした（サーバーに見つかりません）",
      );
    }
  }, [api]);

  useEffect(() => {
    reload().catch((e: unknown) => {
      console.warn("タイムラインの取得に失敗しました", e);
    });
  }, [reload]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await reload();
    } catch (e) {
      console.warn("タイムラインの取得に失敗しました", e);
    } finally {
      setRefreshing(false);
    }
  };

  const onLoadMore = async () => {
    if (nextBefore === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const settings = await loadSettings();
      const result = await api.listObservations(settings, { limit: PAGE_SIZE, before: nextBefore });
      if (result.kind === "ok") {
        setItems((prev) => {
          const known = new Set(prev.map((i) => i.observation_id));
          return [...prev, ...result.data.observations.filter((i) => !known.has(i.observation_id))];
        });
        setNextBefore(result.data.next_before);
        setNotice(null);
      } else if (result.kind === "auth") {
        setNotice("招待コードを確かめてください");
      } else {
        setNotice("続きを取得できませんでした");
      }
    } catch (e) {
      console.warn("続きの取得に失敗しました", e);
      setNotice("続きを取得できませんでした");
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Pressable onPress={onBack} style={styles.smallButton}>
          <Text style={styles.buttonText}>← 撮影に戻る</Text>
        </Pressable>
      </View>
      <FlatList
        data={items}
        keyExtractor={(item) => item.observation_id}
        renderItem={({ item }) => <Item item={item} />}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
        contentContainerStyle={styles.content}
        ListHeaderComponent={
          <View>
            {queueState.pendingCount > 0 ? (
              <Text style={styles.sub}>
                未送信 {queueState.pendingCount} 件（送れたら一覧に出ます）
              </Text>
            ) : null}
            {notice !== null ? <Text style={styles.warn}>{notice}</Text> : null}
          </View>
        }
        ListEmptyComponent={
          notice === null ? <Text style={styles.sub}>まだ記録はありません。</Text> : null
        }
        ListFooterComponent={
          nextBefore !== null ? (
            <Pressable
              onPress={onLoadMore}
              disabled={loadingMore}
              style={[styles.smallButton, styles.more, loadingMore && styles.disabled]}
            >
              <Text style={styles.buttonText}>{loadingMore ? "読み込み中…" : "もっと見る"}</Text>
            </Pressable>
          ) : null
        }
      />
    </View>
  );
}

// ステータスバーの分の余白（SafeAreaView を使わずパディングで済ませる）
const TOP_PADDING = (Platform.OS === "android" ? (StatusBar.currentHeight ?? 24) : 48) + 8;

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#fff" },
  header: { paddingTop: TOP_PADDING, paddingHorizontal: 12, paddingBottom: 8 },
  content: { padding: 12, paddingBottom: 48 },
  item: { flexDirection: "row", gap: 12, borderTopWidth: 1, borderTopColor: "#ddd", paddingVertical: 8 },
  itemBody: { flex: 1 },
  thumb: { width: 80, height: 80, borderRadius: 6, backgroundColor: "#eceff1" },
  thumbEmpty: { alignItems: "center", justifyContent: "center" },
  text: { fontSize: 15, fontWeight: "bold" },
  sub: { fontSize: 13, color: "#555", marginTop: 2 },
  warn: { fontSize: 14, color: "#b26a00", marginVertical: 8 },
  more: { alignSelf: "center", marginTop: 12 },
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
