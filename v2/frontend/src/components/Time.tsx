import { durationBetween, formatDateTime, formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";

/** "3 minutes ago" with the exact local time as tooltip and machine-readable value. */
export function RelativeTime({ iso }: { iso: string }) {
  const now = useNow(true, 30_000);
  return (
    <time dateTime={iso} title={formatDateTime(iso)}>
      {formatRelative(iso, now)}
    </time>
  );
}

export function Duration({
  startedAt,
  finishedAt,
}: {
  startedAt: string | null;
  finishedAt: string | null;
}) {
  const live = startedAt !== null && finishedAt === null;
  const now = useNow(live);
  const text = durationBetween(startedAt, finishedAt, now);
  return text === null ? <span className="muted">-</span> : <span>{text}</span>;
}
