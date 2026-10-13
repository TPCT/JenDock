import type { LogChunk } from "../api/types";

export interface LogEntry {
  text: string;
  /** Byte offset of the next byte the server will send. */
  next: number;
  complete: boolean;
  loading: boolean;
  error: string | null;
  truncated: boolean;
}

export const EMPTY_ENTRY: LogEntry = {
  text: "",
  next: 0,
  complete: false,
  loading: false,
  error: null,
  truncated: false,
};

/** Keeps the browser responsive for very long logs; the full log stays on the server. */
export const MAX_LOG_CHARS = 2_000_000;

export type LogFetcher = (stepId: number, offset: number) => Promise<LogChunk>;

/**
 * Per-step log buffers fed by the event stream and by chunked HTTP loads.
 * Chunks are applied only when they continue exactly where the buffer ends, so a
 * reconnect (which may replay events) can never duplicate or reorder text.
 */
export class LogStore {
  private entries = new Map<number, LogEntry>();
  private listeners = new Set<() => void>();
  private loads = new Map<number, Promise<void>>();
  private watched = new Set<number>();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  get(stepId: number): LogEntry {
    return this.entries.get(stepId) ?? EMPTY_ENTRY;
  }

  private set(stepId: number, patch: Partial<LogEntry>): void {
    this.entries.set(stepId, { ...this.get(stepId), ...patch });
    this.listeners.forEach((listener) => {
      listener();
    });
  }

  /** Returns false when the chunk does not line up and the caller should reload from `next`. */
  apply(stepId: number, chunk: LogChunk): boolean {
    const entry = this.get(stepId);
    if (chunk.next_offset <= entry.next) {
      if (chunk.complete && !entry.complete) this.set(stepId, { complete: true });
      return true;
    }
    if (chunk.offset !== entry.next) return false;
    let text = entry.text + chunk.text;
    let truncated = entry.truncated;
    if (text.length > MAX_LOG_CHARS) {
      text = text.slice(text.length - MAX_LOG_CHARS);
      truncated = true;
    }
    this.set(stepId, { text, next: chunk.next_offset, complete: chunk.complete, truncated });
    return true;
  }

  /** True once someone asked to see this step's log (it is then kept gap-free). */
  isWatched(stepId: number): boolean {
    return this.watched.has(stepId);
  }

  markComplete(stepId: number): void {
    if (!this.get(stepId).complete) this.set(stepId, { complete: true });
  }

  /** Fetches everything after the buffered position, page by page. Concurrent calls share one load. */
  load(stepId: number, fetcher: LogFetcher): Promise<void> {
    this.watched.add(stepId);
    const running = this.loads.get(stepId);
    if (running) return running;
    const task = this.loadPages(stepId, fetcher).finally(() => {
      this.loads.delete(stepId);
    });
    this.loads.set(stepId, task);
    return task;
  }

  private async loadPages(stepId: number, fetcher: LogFetcher): Promise<void> {
    this.set(stepId, { loading: true, error: null });
    try {
      for (;;) {
        const offset = this.get(stepId).next;
        const chunk = await fetcher(stepId, offset);
        this.apply(stepId, chunk);
        if (chunk.complete || chunk.next_offset <= offset) break;
      }
      this.set(stepId, { loading: false });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not load the log.";
      this.set(stepId, { loading: false, error: message });
    }
  }

  clear(): void {
    this.entries = new Map();
    this.loads = new Map();
    this.watched = new Set();
    this.listeners.forEach((listener) => {
      listener();
    });
  }
}
