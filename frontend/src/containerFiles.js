export function fileBreadcrumbs(path = "") {
  if (
    typeof path !== "string" ||
    path.startsWith("/") ||
    path.split("/").some((part) => part === "." || part === "..") ||
    /[\x00-\x1f\x7f]/.test(path)
  )
    throw new Error("The file path must stay within the application root.");
  const segments = path.split("/").filter(Boolean);
  return [
    { label: "Application root", path: "" },
    ...segments.map((label, index) => ({
      label,
      path: segments.slice(0, index + 1).join("/"),
    })),
  ];
}
export function fileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "Size unavailable";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
export function verifyFileResponse(
  result,
  containerId,
  path,
  root = null,
  preview = false,
) {
  if (result?.container_id !== containerId || result.path !== path)
    throw new Error(
      "File response does not match this container and path. Reload its details.",
    );
  if (!preview && result.capability?.supported === false) return result;
  if (typeof result.root !== "string" || (root && result.root !== root))
    throw new Error(
      "The application root changed or could not be verified. Reload container details.",
    );
  if (preview) {
    if (
      typeof result.text !== "string" ||
      new TextEncoder().encode(result.text).length > 65536
    )
      throw new Error(
        "Preview exceeds the text size limit or is incomplete. Reload the directory.",
      );
  } else if (!Array.isArray(result.entries))
    throw new Error("Directory response is incomplete. Reload to try again.");
  return result;
}
