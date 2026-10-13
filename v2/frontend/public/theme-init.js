// Applies the saved colour mode before first paint. External file: the API's CSP forbids inline
// scripts. Primer's CSS variables are selected by three attributes on <html>; the choices are
// auto (follow the system), light, dark and dark_dimmed. Keep in sync with src/lib/theme.ts.
try {
  var saved = window.localStorage.getItem("theme");
  var root = document.documentElement;
  if (saved === "light" || saved === "dark") {
    root.setAttribute("data-color-mode", saved);
  } else if (saved === "dark_dimmed") {
    root.setAttribute("data-color-mode", "dark");
    root.setAttribute("data-dark-theme", "dark_dimmed");
  }
  if (saved === "auto" || saved === "light" || saved === "dark" || saved === "dark_dimmed") {
    root.setAttribute("data-theme-choice", saved);
  }
} catch (_error) {
  /* storage unavailable: follow the system preference */
}
