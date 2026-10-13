/** xterm.js needs a real layout engine; tests replace it with this recording double. */
export class FakeTerminal {
  static instances: FakeTerminal[] = [];
  static get last(): FakeTerminal {
    const instance = FakeTerminal.instances[FakeTerminal.instances.length - 1];
    if (!instance) throw new Error("no terminal was created");
    return instance;
  }

  rows = 24;
  cols = 80;
  options: Record<string, unknown> = {};
  written: string[] = [];
  disposed = false;
  private data: ((data: string) => void) | null = null;
  private resize: ((size: { rows: number; cols: number }) => void) | null = null;

  constructor(options: Record<string, unknown> = {}) {
    this.options = options;
    FakeTerminal.instances.push(this);
  }

  loadAddon(): void {
    /* addons are doubles too */
  }
  open(): void {
    /* no DOM rendering */
  }
  focus(): void {
    /* nothing to focus */
  }
  write(chunk: Uint8Array): void {
    this.written.push(new TextDecoder().decode(chunk));
  }
  onData(callback: (data: string) => void) {
    this.data = callback;
    return { dispose: () => undefined };
  }
  onResize(callback: (size: { rows: number; cols: number }) => void) {
    this.resize = callback;
    return { dispose: () => undefined };
  }
  /** Same as typing: goes through onData. */
  input(data: string): void {
    this.data?.(data);
  }
  type(data: string): void {
    this.data?.(data);
  }
  resizeTo(rows: number, cols: number): void {
    this.rows = rows;
    this.cols = cols;
    this.resize?.({ rows, cols });
  }
  dispose(): void {
    this.disposed = true;
  }
}

export class FakeFitAddon {
  fit(): void {
    /* no layout */
  }
}

export class FakeWebglAddon {
  onContextLoss(): void {
    /* no GPU */
  }
  dispose(): void {
    /* nothing */
  }
}
