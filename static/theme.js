"use strict";
// Apply before CSS loads to avoid a light flash for dark-theme users.
(() => {
  const key = "much-ado-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference;
  try { preference = localStorage.getItem(key); } catch { /* Storage may be disabled. */ }
  if (!["light", "dark"].includes(preference)) preference = null;

  function apply() {
    const dark = preference ? preference === "dark" : system.matches;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const button = document.getElementById("themeToggle");
    if (button) {
      button.setAttribute("aria-pressed", String(dark));
      button.title = dark ? "Switch to light mode" : "Switch to dark mode";
    }
  }

  apply();
  system.addEventListener("change", () => { if (!preference) apply(); });
  window.addEventListener("storage", event => {
    if (event.key !== key && event.key !== null) return;
    preference = ["light", "dark"].includes(event.newValue) ? event.newValue : null;
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("themeToggle").addEventListener("click", () => {
      preference = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem(key, preference); } catch { /* Keep the in-memory choice. */ }
      apply();
    });
  });
})();
