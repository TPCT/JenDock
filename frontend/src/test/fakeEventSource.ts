/** Minimal EventSource double: tests push events and connection errors by hand. */
export class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  static install(): void {
    FakeEventSource.instances = [];
    (globalThis as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  }

  static get last(): FakeEventSource {
    const instance = FakeEventSource.instances[FakeEventSource.instances.length - 1];
    if (!instance) throw new Error("no EventSource was opened");
    return instance;
  }

  readonly url: string;
  readonly withCredentials: boolean;
  readyState = 0;
  private listeners = new Map<string, ((event: Event) => void)[]>();

  constructor(url: string | URL, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = init?.withCredentials ?? false;
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: (event: Event) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }

  close(): void {
    this.readyState = 2;
  }

  private dispatch(name: string, event: Event): void {
    this.listeners.get(name)?.forEach((listener) => {
      listener(event);
    });
  }

  open(): void {
    this.readyState = 1;
    this.dispatch("open", new Event("open"));
  }

  emit(name: string, payload: unknown): void {
    this.dispatch(name, new MessageEvent(name, { data: JSON.stringify(payload) }));
  }

  fail(): void {
    this.readyState = 0;
    this.dispatch("error", new Event("error"));
  }
}
