import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { fetchSession } from "./client";
import { fetchLog, keys } from "./hooks";
import type { RunEventMap, RunStatus } from "./types";
import { LogStore } from "../lib/logStore";
import { isTerminal } from "../lib/format";

export type Connection = "idle" | "connecting" | "live" | "stale" | "closed";

function parse(event: Event): unknown {
  try {
    return JSON.parse((event as MessageEvent<string>).data);
  } catch {
    return null;
  }
}

/**
 * Subscribes to `GET /api/runs/{id}/events` while the run is not finished.
 * The server sends a full `status` on every (re)connect; EventSource reconnects by itself,
 * so a drop only flips `connection` to "stale" until the next snapshot arrives.
 */
export function useRunStream(runId: number, status: RunStatus | undefined) {
  const client = useQueryClient();
  const store = useMemo(() => new LogStore(), []);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [attempt, setAttempt] = useState(0);
  const finished = status === undefined ? undefined : isTerminal(status);

  useEffect(() => {
    store.clear();
  }, [runId, store]);

  useEffect(() => {
    if (finished !== false) return;
    const source = new EventSource(`/api/runs/${runId}/events`, { withCredentials: true });

    const listen = <K extends keyof RunEventMap>(
      name: K,
      handler: (payload: RunEventMap[K]) => void,
    ) => {
      source.addEventListener(name, (event) => {
        const payload = parse(event);
        if (payload !== null) handler(payload as RunEventMap[K]);
      });
    };

    source.addEventListener("open", () => {
      setConnection("live");
    });
    source.addEventListener("error", () => {
      setConnection("stale");
      // A dead session cannot be told apart from a network error here: probing it triggers
      // the shared 401 redirect when the cookie is gone.
      fetchSession().catch(() => undefined);
    });
    listen("status", (run) => {
      setConnection("live");
      client.setQueryData(keys.run(runId), run);
    });
    listen("log", ({ step_id: stepId, offset, text }) => {
      const next = offset + new TextEncoder().encode(text).length;
      const chunk = { offset, next_offset: next, size: next - offset, text, complete: false };
      if (!store.apply(stepId, chunk) && store.isWatched(stepId)) {
        void store.load(stepId, (id, from) => fetchLog(runId, id, from));
      }
    });
    listen("end", () => {
      source.close();
      setConnection("closed");
      void client.invalidateQueries({ queryKey: keys.run(runId) });
      void client.invalidateQueries({ queryKey: ["runs"] });
    });

    return () => {
      source.close();
    };
  }, [runId, finished, attempt, client, store]);

  return {
    store,
    connection: finished === undefined ? "idle" : finished ? "closed" : connection,
    reconnect: () => {
      setConnection("connecting");
      setAttempt((value) => value + 1);
    },
  };
}
