/* Throwaway harness: execute app.js against a minimal DOM stub and drive the render paths.
   Catches reference errors and typos that a static `node --check` cannot. Deleted after use. */
import { readFileSync } from "node:fs";
import vm from "node:vm";

const made = [];
function el(tag = "div") {
  const node = {
    tagName: String(tag).toUpperCase(), _html: "", children: [], style: new Map(),
    dataset: {}, classList: { _s: new Set(), add(...c) { c.forEach(x => x && this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); }, toggle(c) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); },
      contains(c) { return this._s.has(c); } },
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    get textContent() { return this._text || ""; }, set textContent(v) { this._text = String(v); },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild(c) { this.children.push(c); return c; }, remove() {},
    insertAdjacentHTML() {}, insertAdjacentElement() {}, focus() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, addEventListener() {}, onclick: null,
    scrollIntoView() {}, isConnected: true,
  };
  node.style.setProperty = (k, v) => node.style.set(k, v);
  made.push(node);
  return node;
}

/* A container that answers querySelector for the classes the render paths look up, so the
   harness exercises the real branch rather than the null-guard branch. */
function container() {
  const n = el("div");
  const words = Array.from({ length: 8 }, () => el("span"));
  n.querySelector = () => null;
  n.querySelectorAll = sel => (sel === ".lp__w" ? words : []);
  return n;
}

const doc = {
  body: el("body"),
  createElement: el,
  getElementById: () => null,
  querySelector: sel => {
    if (sel === "#app" || sel === "#main") return doc._app;
    if (sel === ".lp2") return doc._lp;
    if (sel === "#band") return doc._band;
    if (sel === "#lform") return doc._form;
    return null;
  },
  querySelectorAll: () => [],
  addEventListener: () => {},
  documentElement: el("html"),
};
doc._app = el("div");
doc._lp = container();
doc._band = container();
doc._form = el("form");

const store = () => { const m = new Map(); return {
  getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)),
  removeItem: k => m.delete(k), clear: () => m.clear() }; };

const calls = [];
const sandbox = {
  document: doc, console,
  window: {
    matchMedia: () => ({ matches: false }),
    addEventListener: () => {}, location: { pathname: "/" },
  },
  location: { pathname: "/" },
  history: { pushState() {}, replaceState() {} },
  sessionStorage: store(), localStorage: store(),
  performance: { now: () => 0 },
  requestAnimationFrame: () => 0,
  setTimeout: () => 0, clearTimeout: () => {},
  alert: () => {}, confirm: () => true, prompt: () => "x",
  navigator: { userAgent: "node" },
  TextDecoder: class { decode() { return ""; } },
  Blob: class {}, File: class {}, FormData: class { append() {} },
  URL: { createObjectURL: () => "blob:", revokeObjectURL: () => {} },
  fetch: async (path) => { calls.push(path); throw new Error("network disabled in smoke test"); },
};
sandbox.window.location = sandbox.location;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
const code = readFileSync("web/app.js", "utf8");
vm.runInContext(code, ctx, { filename: "app.js" });

const g = ctx;
const results = [];
function tryRun(label, fn) {
  try { fn(); results.push(["ok  ", label]); }
  catch (e) { results.push(["FAIL", `${label} :: ${e.message}`]); }
}

// The two unauthenticated entry points, which is where the new work lives.
tryRun("renderLanding()", () => g.renderLanding());
tryRun("renderLogin()", () => g.renderLogin());

// The new pure components, called with realistic shapes.
const score = { overall: 78, status: "Needs attention", assessed_dimensions: 4,
  total_dimensions: 6, dimensions: [
    { key: "property", label: "Property", score: 62 },
    { key: "people", label: "People", score: 88 },
    { key: "operations", label: "Operations", score: null },
    { key: "liability", label: "Liability", score: 35 }] };
const blind = { coverage: { areas_we_can_speak_to: 4, areas_total: 6 } };
const signals = { events: [{ label: "Inventory increased 50%", kind: "STOCK_UP" }] };
const sentinel = { environment: { level: "ELEVATED", label: "elevated",
  summary: "Heavy rain forecast" }, live: { any_live: true } };

tryRun("protectionField(full)", () => g.protectionField(score, blind, signals, sentinel));
tryRun("protectionField(empty)", () => g.protectionField({ dimensions: [] }, null, null, null));
tryRun("convergence(both)", () => g.convergencePanel(signals, sentinel, null));
tryRun("convergence(connect only)", () => g.convergencePanel(signals, { environment: { level: "LOW" } }, null));
tryRun("convergence(sentinel only)", () => g.convergencePanel({ events: [] }, sentinel, null));
tryRun("convergence(neither)", () => g.convergencePanel({ events: [] }, { environment: { level: "LOW" } }, null));
tryRun("convergence(nothing)", () => g.convergencePanel(null, null, null));
["DRAFTED", "SENT_TO_ADVISER", "ADVISER_REVIEWING", "INFORMATION_REQUESTED",
 "QUOTE_RECEIVED", "BOUND", "DECLINED", "WITHDRAWN", "NOT_REQUIRED", "WEIRD"]
  .forEach(s => tryRun(`workflowRail(${s})`, () => {
    const html = g.workflowRail(s);
    if (!html.includes("wr__step")) throw new Error("no steps rendered");
  }));
tryRun("priorityCanvas(rows)", () => g.priorityCanvas([
  { business_name: "Hock Lee Foods", score: 62, needs_review_count: 3, priority: "HIGH", demo: false },
  { business_name: "Kimly Metalworks", score: 58, needs_review_count: 2, priority: "HIGH", demo: true },
  { business_name: "Bugis Print", score: 88, needs_review_count: 0, priority: "LOW", demo: true }]));
tryRun("priorityCanvas(empty)", () => g.priorityCanvas([]));
tryRun("verisOrb()", () => { if (!g.verisOrb().includes("<svg")) throw new Error("no svg"); });
tryRun("splitWords()", () => {
  const out = g.splitWords("Insurance that keeps up");
  if ((out.match(/lp__w/g) || []).length !== 4) throw new Error("wrong word count");
});
tryRun("workflowStage mapping", () => {
  const m = { DRAFTED: 0, SENT_TO_ADVISER: 1, QUOTE_RECEIVED: 2, BOUND: 3, DECLINED: -1 };
  for (const [k, v] of Object.entries(m))
    if (g.workflowStage(k) !== v) throw new Error(`${k} -> ${g.workflowStage(k)}, expected ${v}`);
});
// A figure that must NOT animate: an em dash means "not established".
tryRun("animateFigures leaves em dash alone", () => {
  const n = el("div"); n.textContent = "—"; n._text = "—";
  const root = { querySelectorAll: () => [n] };
  g.animateFigures(root);
  if (n.dataset.to !== undefined) throw new Error("em dash was coerced to a number");
});
tryRun("animateFigures parses S$ and %", () => {
  const a = el("div"); a._text = "S$150,000";
  const b = el("div"); b._text = "66.7%";
  g.animateFigures({ querySelectorAll: () => [a, b] });
  if (a.dataset.to !== "150000" || a.dataset.prefix !== "S$") throw new Error("money parse wrong: " + JSON.stringify(a.dataset));
  if (b.dataset.to !== "66.7" || b.dataset.suffix !== "%") throw new Error("percent parse wrong: " + JSON.stringify(b.dataset));
});

// ── the degraded worked example says so ────────────────────────────────────
// The server reports that the published base wording is not in the corpus, and three of
// the example's findings quote it. Rendered, not just computed.
const NOTICE_KEY = "veris_example_wording_notice";
tryRun("exampleWordingNotice is empty when the corpus holds the wording", () => {
  sandbox.sessionStorage.removeItem(NOTICE_KEY);
  if (g.exampleWordingNotice() !== "") throw new Error("a notice was rendered with nothing stored");
});
tryRun("exampleWordingNotice renders the server's sentence", () => {
  sandbox.sessionStorage.setItem(NOTICE_KEY,
    "The published base wording is not in the corpus on this machine, so the example "
    + "runs on its synthetic documents alone.");
  const html = g.exampleWordingNotice();
  if (!html.includes("not in the corpus on this machine")) throw new Error("the notice text is missing");
  if (!html.includes("without its published base wording")) throw new Error("no heading");
});
tryRun("exampleWordingNotice escapes server-derived text", () => {
  sandbox.sessionStorage.setItem(NOTICE_KEY, '<script>alert("x")</script>');
  const html = g.exampleWordingNotice();
  if (html.includes("<script>")) throw new Error("server text reached the DOM unescaped");
  if (!html.includes("&lt;script&gt;")) throw new Error("not escaped through esc()");
  sandbox.sessionStorage.removeItem(NOTICE_KEY);
});

// ── a third party's document: the cited paragraph and a publisher link, never a page ──
const PARA = "4.2 Average. <script>x</script> the amount payable shall be proportionately reduced.";
const QUOTE = "the amount payable shall be proportionately reduced";
const excerptPayload = (publisher) => ({
  display: "EXCERPT", available: false, filename: "01-policy-wording.pdf",
  page_no: 15, page_count: 28,
  reason: "This is a published document by <b>A Publisher</b>.",
  excerpt: { text: PARA, quote_start: PARA.indexOf(QUOTE),
    quote_end: PARA.indexOf(QUOTE) + QUOTE.length, cut_before: false, cut_after: true,
    page_no: 15 },
  publisher,
});
tryRun("drawerExcerpt marks the quotation, escapes the paragraph, links the publisher", () => {
  const html = g.drawerExcerpt(excerptPayload({ available: true, title: "A Wording",
    url: "https://publisher.example/wording.pdf", publisher: "A Publisher" }));
  if (!html.includes(`<mark>${QUOTE}</mark>`)) throw new Error("the quotation is not marked");
  if (html.includes("<script>")) throw new Error("paragraph text reached the DOM unescaped");
  if (!html.includes("&lt;script&gt;")) throw new Error("paragraph not escaped through esc()");
  if (!html.includes("&lt;b&gt;A Publisher")) throw new Error("reason not escaped");
  if (!html.includes('href="https://publisher.example/wording.pdf"')) throw new Error("no publisher link");
  if (!html.includes('rel="noopener noreferrer"')) throw new Error("link keeps an opener");
  if (html.includes("<img")) throw new Error("a page image was drawn for a published document");
  if (!html.includes(" …")) throw new Error("a capped paragraph is not marked as cut");
});
tryRun("drawerExcerpt draws no link for a non-http address", () => {
  const html = g.drawerExcerpt(excerptPayload({ available: true, url: "javascript:alert(1)" }));
  if (html.includes("javascript:")) throw new Error("a script URL reached an href");
  if (html.includes("<a ")) throw new Error("a link was drawn for an unsafe address");
});
tryRun("drawerExcerpt says when no address was recorded", () => {
  const html = g.drawerExcerpt(excerptPayload({ available: false, url: null,
    note: "The address this document was retrieved from was not recorded." }));
  if (!html.includes("was not recorded")) throw new Error("the missing address is not stated");
  if (html.includes("<a ")) throw new Error("a link was invented");
});
tryRun("drawerPagePane routes an EXCERPT payload to the paragraph view", () => {
  const html = g.drawerPagePane({
    finding: { spans: [{ document_id: "doc-1", page_no: 15, span_id: "s1" }] },
    spanIndex: 0,
    page: excerptPayload({ available: true, url: "https://publisher.example/w.pdf" }) });
  if (!html.includes("The paragraph it came from")) throw new Error("wrong heading");
  if (!html.includes("<mark>")) throw new Error("no marked quotation");
  if (html.includes("<img") || html.includes("pagefull-open"))
    throw new Error("a page image or full-screen control was drawn");
});

// ── the guided tour ────────────────────────────────────────────────────────
tryRun("every role has tour steps", () => {
  for (const role of ["SME", "ADVISER", "CARRIER"]) {
    const steps = g.tourStepsFor(role);
    if (!steps.length) throw new Error(`${role} has no steps`);
    steps.forEach((s, i) => {
      if (!s.target) throw new Error(`${role}[${i}] has no target selector`);
      if (!s.title || !s.body) throw new Error(`${role}[${i}] has no copy`);
      if (s.tab && (!Array.isArray(s.tab) || s.tab.length !== 2))
        throw new Error(`${role}[${i}] tab must be [group, id]`);
      if (s.place && !["top", "bottom", "left", "right"].includes(s.place))
        throw new Error(`${role}[${i}] bad placement ${s.place}`);
    });
  }
});
tryRun("tour steps name real views", () => {
  // A step that navigates to a view id the app does not route to would strand the tour.
  for (const role of ["SME", "ADVISER", "CARRIER"]) {
    for (const s of g.tourStepsFor(role)) {
      if (!s.view) continue;
      if (!code.includes(`"${s.view}":`) && !code.includes(`id: "${s.view}"`))
        throw new Error(`${role}: view ${s.view} is not a routed destination`);
    }
  }
});
tryRun("tour steps name real tabs", () => {
  // `tab: [group, id]` must match a tab actually declared in that container, or the step
  // navigates to a container and then highlights nothing.
  for (const role of ["SME", "ADVISER", "CARRIER"]) {
    for (const s of g.tourStepsFor(role)) {
      if (!s.tab) continue;
      const [group, id] = s.tab;
      if (!code.includes(`group: "${group}"`))
        throw new Error(`${role}: tab group ${group} does not exist`);
      if (!code.includes(`id: "${id}"`))
        throw new Error(`${role}: tab ${group}/${id} does not exist`);
    }
  }
});
tryRun("tourStepsFor returns copies", () => {
  const a = g.tourStepsFor("SME"), b = g.tourStepsFor("SME");
  a[0]._skip = true;
  if (b[0]._skip) throw new Error("step objects are shared between runs");
});
tryRun("tourStepsFor falls back for an unknown role", () => {
  if (!g.tourStepsFor("NOPE").length) throw new Error("unknown role got no steps");
});
tryRun("SME tour is ten steps", () => {
  const n = g.tourStepsFor("SME").length;
  if (n !== 10) throw new Error(`expected 10, got ${n}`);
});
tryRun("tourInvite is a no-op with no user", () => { g.tourInvite(); });
tryRun("tourEnd is safe when no tour is running", () => { g.tourEnd(false); });
tryRun("tourPaint is safe with no host", () => { g.tourPaint(); });

let bad = 0;
for (const [tag, label] of results) { if (tag === "FAIL") bad++; console.log(tag, label); }
console.log(`\n${results.length - bad}/${results.length} passed`);
console.log("fetches attempted by renderLanding:", calls.join(", ") || "(none)");
const exitCode = bad ? 1 : 0;

/* Optional structural dump: `node tools/smoke_frontend.mjs --dump` prints the landing
   markup so its composition can be eyeballed without a browser. */
if (process.argv.includes("--dump")) {
  g.renderLanding();
  const html = doc._app.innerHTML;
  const want = [
    ["serif wordmark", 'class="lp2__mark"'],
    ["split headline", 'class="lp__w"'],
    ["enter workspace", "Enter workspace"],
    ["orb svg", "<svg class=\"orb\""],
    ["orbit labels", "BUSINESS DATA"],
    ["jade lens", "lensJade"],
    ["amber lens", "lensAmber"],
    ["agent tags", "lp2__tag--jade"],
    ["measured band slot", 'id="band"'],
    ["three cards", "lp2__c--field"],
    ["sign in action", 'data-act="show-login"'],
  ];
  for (const [label, needle] of want) {
    console.log((html.includes(needle) ? "  ok   " : "  MISS ") + label);
  }
  const words = (html.match(/class="lp__w"/g) || []).length;
  console.log(`  headline words: ${words}`);
  console.log(`  landing markup: ${html.length} bytes`);
}

process.exit(exitCode);
