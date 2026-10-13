import { useCallback, useEffect, useRef, type RefObject } from "react";

/**
 * After a reorder the moved card gets a new index; this puts keyboard focus back on the button
 * that was used (`data-focus-key`), so repeated key presses keep moving the same card.
 */
export function useFocusAfterMove(container: RefObject<HTMLElement | null>) {
  const pending = useRef<string | null>(null);
  useEffect(() => {
    const key = pending.current;
    if (key === null) return;
    pending.current = null;
    const target = container.current?.querySelector<HTMLElement>(`[data-focus-key="${key}"]`);
    // A button at the list edge is disabled; fall back to its sibling direction.
    if (target && !(target instanceof HTMLButtonElement && target.disabled)) target.focus();
    else {
      const sibling = container.current?.querySelector<HTMLElement>(
        `[data-focus-key="${key.replace(/-(up|down)$/, (_, d: string) => (d === "up" ? "-down" : "-up"))}"]`,
      );
      sibling?.focus();
    }
  });
  return useCallback((key: string) => {
    pending.current = key;
  }, []);
}
