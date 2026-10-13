import { useSyncExternalStore } from "react";

/** Matches below the desktop breakpoint, where the sidebar becomes an off-canvas drawer. */
export const MOBILE_QUERY = "(max-width: 47.99rem)";

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (callback) => {
      const media = window.matchMedia(query);
      media.addEventListener("change", callback);
      return () => {
        media.removeEventListener("change", callback);
      };
    },
    () => window.matchMedia(query).matches,
  );
}
