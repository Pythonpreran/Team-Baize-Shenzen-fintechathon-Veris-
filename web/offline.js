"use strict";
/* The offline page's only behaviour: show its three sentences in the reader's chosen
 * language from the cached catalogue. The English text is already in the document, so a
 * missing catalogue changes nothing. No request here reaches /api/. */
(async () => {
  let locale = "en";
  try { locale = localStorage.getItem("veris_locale") || "en"; } catch (_) { /* unavailable */ }
  const file = locale === "zh-CN" ? "zh-CN" : "en";
  let catalogue = {};
  try {
    const response = await fetch(`/static/i18n/${file}.json`, { credentials: "same-origin" });
    if (response.ok) catalogue = await response.json();
  } catch (_) { return; }
  const t = key => String(key).split(".").reduce(
    (value, part) => (value && typeof value === "object" ? value[part] : undefined), catalogue);
  const text = {
    "offline.title": t("offline.title"),
    "offline.body": t("offline.body"),
    "offline.retry": t("offline.retry"),
  };
  document.documentElement.lang = file === "zh-CN" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach(node => {
    const value = text[node.dataset.i18n];
    if (typeof value === "string" && value) node.textContent = value;
  });
})();
