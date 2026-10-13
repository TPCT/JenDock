import { useEffect, useRef } from "react";

/** `callback` runs `delayMs` after `value` last changed; the first render does not run it. */
export function useDebouncedEffect(
  value: string,
  delayMs: number,
  callback: (value: string) => void,
): void {
  const latest = useRef(callback);
  const first = useRef(true);
  useEffect(() => {
    latest.current = callback;
  });
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const timer = window.setTimeout(() => {
      latest.current(value);
    }, delayMs);
    return () => {
      window.clearTimeout(timer);
    };
  }, [value, delayMs]);
}
