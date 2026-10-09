// 再送キューを動かすきっかけ（起動、前面に戻ったとき、電波が戻ったとき、30 秒ごと）をつなぐフック。
// 仕様は docs/m3-app-capture.md の 4.3 節。
import { useEffect, useMemo, useState } from "react";
import { AppState } from "react-native";
import { addNetworkStateListener, getNetworkStateAsync } from "expo-network";
import { cleanupIncomplete, observationStore } from "./observationStore";
import { loadSettings } from "./settings";
import { createFetchTransport } from "./transport";
import { createUploadQueue, type QueueState, type UploadQueue } from "./uploadQueue";

/** 期限を過ぎた pending を見にいく間隔（ミリ秒） */
const DUE_INTERVAL_MS = 30_000;

/** キューの Promise が失敗しても握りつぶさず、警告に出す。 */
export function warnOnFailure(task: Promise<unknown>, what: string): void {
  task.catch((e: unknown) => {
    console.warn(`${what}に失敗しました`, e);
  });
}

export function useUploadQueue(): { queue: UploadQueue; state: QueueState } {
  const queue = useMemo(
    () =>
      createUploadQueue({
        store: observationStore,
        transport: createFetchTransport(),
        getSettings: loadSettings,
        now: Date.now,
      }),
    [],
  );
  const [state, setState] = useState<QueueState>(queue.getState());

  useEffect(() => {
    const unsubscribe = queue.subscribe(setState);
    setState(queue.getState());

    // 起動したときは、掃除をしてから送る
    warnOnFailure(
      cleanupIncomplete()
        .catch((e: unknown) => {
          console.warn("保存の途中で残ったデータの掃除に失敗しました", e);
        })
        .then(() => queue.runNow()),
      "起動時の送信",
    );

    const appStateSubscription = AppState.addEventListener("change", (next) => {
      if (next === "active") warnOnFailure(queue.runNow(), "前面に戻ったときの送信");
    });

    // 「つながっていない」から「つながった」に変わったときだけ送る
    // 起動時の状態は、最初の通知より先に取っておく（起動時に圏外だった場合に備える）
    let wasConnected: boolean | null = null;
    getNetworkStateAsync()
      .then((initial) => {
        if (wasConnected === null) wasConnected = initial.isConnected === true;
      })
      .catch((e: unknown) => {
        console.warn("ネットワークの状態の取得に失敗しました", e);
      });
    const networkSubscription = addNetworkStateListener((event) => {
      const connected = event.isConnected === true;
      if (connected && wasConnected === false) {
        warnOnFailure(queue.runNow(), "電波が戻ったときの送信");
      }
      wasConnected = connected;
    });

    const timer = setInterval(() => {
      warnOnFailure(queue.runDue(), "定期的な送信");
    }, DUE_INTERVAL_MS);

    return () => {
      unsubscribe();
      appStateSubscription.remove();
      networkSubscription.remove();
      clearInterval(timer);
    };
  }, [queue]);

  return { queue, state };
}
