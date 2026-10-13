/** Accepts only in-app absolute paths so a login link can never redirect off-site. */
export function safeNext(value: string | null): string {
  if (value === null || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return "/";
  }
  if (value.startsWith("/login")) return "/";
  return value;
}
