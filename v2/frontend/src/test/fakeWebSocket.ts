/** WebSocket double: tests open it, push frames and close it with a code by hand. */
import { vi } from "vitest";

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  static install(): void {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  }

  static uninstall(): void {
    vi.unstubAllGlobals();
  }

  static get last(): FakeWebSocket {
    const instance = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    if (!instance) throw new Error("no WebSocket was opened");
    return instance;
  }

  readonly url: string;
  binaryType = "blob";
  readyState = 0;
  sent: (string | Uint8Array)[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string | ArrayBuffer>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    FakeWebSocket.instances.push(this);
  }

  send(data: string | Uint8Array): void {
    this.sent.push(data);
  }

  /** Text frames the browser sent, parsed. */
  get json(): Record<string, unknown>[] {
    return this.sent.flatMap((item) =>
      typeof item === "string" ? [JSON.parse(item) as Record<string, unknown>] : [],
    );
  }

  /** Binary frames the browser sent, decoded as text. */
  get keystrokes(): string {
    return this.sent
      .flatMap((item) => (typeof item === "string" ? [] : [new TextDecoder().decode(item)]))
      .join("");
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  text(message: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(message) }));
  }

  binary(data: string): void {
    const bytes = new TextEncoder().encode(data);
    const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    this.onmessage?.(new MessageEvent("message", { data: buffer }));
  }

  close(code = 1000): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close", { code }));
  }

  /** What the server does: end the connection. */
  serverClose(code: number): void {
    this.close(code);
  }
}
