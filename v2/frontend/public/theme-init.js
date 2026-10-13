// Applies the saved theme before first paint. External file: the API's CSP forbids inline scripts.
try {
  var saved = window.localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.setAttribute("data-theme", saved);
  }
} catch (_error) {
  /* storage unavailable: follow the system preference */
}
