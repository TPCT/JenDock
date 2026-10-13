import styles from "./Initials.module.css";

/** Two letters: the first letters of a two-word display name, else the first two of the name. */
export function initialsOf(displayName: string | null | undefined, username: string): string {
  const words = (displayName ?? "").trim().split(/\s+/).filter(Boolean);
  const first = words[0];
  const last = words[words.length - 1];
  if (first && last && words.length >= 2) {
    return `${first.charAt(0)}${last.charAt(0)}`.toUpperCase();
  }
  return (first ?? username)
    .replace(/[^\p{L}\p{N}]/gu, "")
    .slice(0, 2)
    .toUpperCase();
}

/** A round avatar showing initials, GitHub's fallback when a user has no picture. */
export function Initials({
  displayName,
  username,
  size = 24,
}: {
  displayName?: string | null;
  username: string;
  size?: 24 | 32 | 64 | 96;
}) {
  return (
    <span
      className={`${styles.avatar} ${styles[`size${String(size)}`] ?? ""}`}
      aria-hidden="true"
      data-testid="initials"
    >
      {initialsOf(displayName, username)}
    </span>
  );
}
