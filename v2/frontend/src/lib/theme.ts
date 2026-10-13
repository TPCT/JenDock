import { useCallback, useSyncExternalStore } from "react";

/**
 * Colour mode, expressed the way Primer's CSS variables select it: three attributes on <html>
 * (`data-color-mode`, `data-light-theme`, `data-dark-theme`). `auto` follows the system.
 * `public/theme-init.js` applies the saved choice before first paint; keep both in sync.
 */
export type ThemeChoice = "auto" | "light" | "dark" | "dark_dimmed";
export const THEME_CHOICES: readonly { value: ThemeChoice; label: string }[] = [
  { value: "auto", label: "Sync with system" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "dark_dimmed", label: "Dark dimmed" },
];

const KEY = "theme";
const listeners = new Set<() => void>();

function isChoice(value: string | null): value is ThemeChoice {
  return value === "auto" || value === "light" || value === "dark" || value === "dark_dimmed";
}

export function readChoice(): ThemeChoice {
  const attr = document.documentElement.getAttribute("data-theme-choice");
  return isChoice(attr) ? attr : "auto";
}

export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  root.setAttribute("data-theme-choice", choice);
  root.setAttribute("data-color-mode", choice === "dark_dimmed" ? "dark" : choice);
  root.setAttribute("data-light-theme", "light");
  root.setAttribute("data-dark-theme", choice === "dark_dimmed" ? "dark_dimmed" : "dark");
}

function subscribe(callback: () => void): () => void {
  listeners.add(callback);
  return () => {
    listeners.delete(callback);
  };
}

export function useTheme(): { choice: ThemeChoice; setChoice: (choice: ThemeChoice) => void } {
  const choice = useSyncExternalStore(subscribe, readChoice);
  const setChoice = useCallback((next: ThemeChoice) => {
    applyTheme(next);
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      /* storage blocked: the choice lasts for this page view */
    }
    listeners.forEach((listener) => {
      listener();
    });
  }, []);
  return { choice, setChoice };
}
