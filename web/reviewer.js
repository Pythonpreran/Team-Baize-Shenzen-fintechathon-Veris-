"use strict";

const root = document.getElementById("reviewer-console");
const messages = {};

function escText(value) { return String(value ?? ""); }
function message(key) {
  return key.split(".").reduce((current, part) => current && typeof current === "object" ? current[part] : undefined, messages);
}
function t(key, params = {}) {
  const template = typeof message(key) === "string" ? message(key) : key;
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (_all, name) => escText(params[name]));
}
function csrfToken() {
  const prefix = "veris_csrf=";
  return document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith(prefix))?.slice(prefix.length) || "";
}
async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  const body = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) throw new Error(body.detail || body.error?.message || body.error || response.statusText);
  return body;
}
function element(tag, text = "") { const node = document.createElement(tag); node.textContent = text; return node; }
function card() { const node = document.createElement("section"); node.className = "reviewer-card"; return node; }
function tag(text) { const node = element("span", text); node.className = "reviewer-tag"; return node; }
function status(text, error = false) { const node = element("p", text); node.className = error ? "reviewer-error" : "reviewer-status"; return node; }

function heading() {
  const node = card();
  node.append(element("h1", t("reviewer.title")), element("p", t("reviewer.lead")));
  return node;
}
function addMeta(node, finding) {
  const meta = document.createElement("div"); meta.className = "reviewer-meta";
  meta.append(tag(t("reviewer.source", { source: finding.source })),
              tag(t("reviewer.state", { state: finding.output_state })),
              tag(t("reviewer.class", { evidenceClass: finding.evidence_class })));
  node.append(meta);
}
function reviewForm(finding, onSaved) {
  const form = document.createElement("form"); form.className = "reviewer-form";
  const decisionLabel = element("label", t("reviewer.decision"));
  const decision = document.createElement("select");
  decision.name = "decision";
  [["AGREE", t("reviewer.agree")], ["DISAGREE", t("reviewer.disagree")]].forEach(([value, label]) => {
    const option = document.createElement("option"); option.value = value; option.textContent = label; decision.append(option);
  });
  decisionLabel.append(decision);
  const severityLabel = element("label", t("reviewer.severity"));
  const severity = document.createElement("select"); severity.name = "severity";
  [["LOW", t("reviewer.low")], ["MODERATE", t("reviewer.moderate")], ["HIGH", t("reviewer.high")]].forEach(([value, label]) => {
    const option = document.createElement("option"); option.value = value; option.textContent = label; severity.append(option);
  });
  severityLabel.append(severity);
  const commentLabel = element("label", t("reviewer.comment"));
  const comment = document.createElement("textarea"); comment.name = "comment"; comment.placeholder = t("reviewer.comment_placeholder");
  commentLabel.append(comment);
  const submit = element("button", t("reviewer.submit")); submit.type = "submit";
  form.append(decisionLabel, severityLabel, commentLabel, submit);
  form.addEventListener("submit", async event => {
    event.preventDefault(); submit.disabled = true;
    try {
      await api(`/api/reviewer/findings/${encodeURIComponent(finding.id)}/review`, {
        method: "POST", body: { decision: decision.value, severity: severity.value, comment: comment.value },
      });
      onSaved();
    } catch (error) { form.append(status(error.message, true)); submit.disabled = false; }
  });
  return form;
}
function findingCard(finding, onSaved) {
  const node = card();
  node.append(element("h2", t("reviewer.finding")), element("h3", finding.headline || finding.rule_id));
  addMeta(node, finding);
  node.append(element("h3", t("reviewer.evidence")));
  const evidence = finding.evidence || [];
  if (!evidence.length) node.append(element("p", "—"));
  evidence.forEach(item => {
    const quote = element("div", item.quote || "—"); quote.className = "reviewer-evidence"; node.append(quote);
  });
  if (finding.review) node.append(status(t("reviewer.saved")));
  else node.append(reviewForm(finding, onSaved));
  return node;
}
async function render() {
  root.replaceChildren(heading());
  try {
    const me = await api("/api/auth/me");
    if (!me.authenticated) { const pane = card(); pane.append(status(t("reviewer.sign_in"), true)); root.append(pane); return; }
    if (me.user.role !== "REVIEWER") { const pane = card(); pane.append(status(t("reviewer.role_required"), true)); root.append(pane); return; }
    const queue = await api("/api/reviewer/queue");
    const controls = card();
    const exportButton = element("button", t("reviewer.export")); exportButton.className = "reviewer-action";
    const exportBox = document.createElement("pre"); exportBox.className = "reviewer-pre"; exportBox.hidden = true;
    exportButton.addEventListener("click", async () => {
      try { const report = await api("/api/reviewer/export"); exportBox.hidden = false; exportBox.textContent = JSON.stringify(report.labels, null, 2) || t("reviewer.export_none"); }
      catch (error) { exportBox.hidden = false; exportBox.textContent = error.message; }
    });
    controls.append(element("h2", t("reviewer.export_title")), exportButton, exportBox); root.append(controls);
    if (!queue.findings.length) { const pane = card(); pane.append(element("p", t("reviewer.empty"))); root.append(pane); return; }
    queue.findings.forEach(finding => root.append(findingCard(finding, render)));
  } catch (error) { const pane = card(); pane.append(status(error.message, true)); root.append(pane); }
}
async function main() {
  try { Object.assign(messages, await (await fetch("/static/i18n/en.json", { credentials: "same-origin" })).json()); }
  catch (_error) { /* Translation fallback keeps identifiers visible for diagnosis. */ }
  document.title = t("reviewer.title");
  render();
}
main();
