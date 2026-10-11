/* Throwaway harness: execute app.js against a minimal DOM stub and drive the render paths.
   Catches reference errors and typos that a static `node --check` cannot. Deleted after use. */
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ENGLISH_CATALOGUE = JSON.parse(readFileSync("web/i18n/en.json", "utf8"));

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
  fetch: async (path) => {
    calls.push(path);
    if (path === "/static/i18n/en.json") {
      return { status: 200, ok: true, json: async () => ENGLISH_CATALOGUE };
    }
    throw new Error("network disabled in smoke test");
  },
};
sandbox.window.location = sandbox.location;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
const code = readFileSync("web/app.js", "utf8");
vm.runInContext(code, ctx, { filename: "app.js" });
await sandbox.window.verisI18nReady;

const g = ctx;
const results = [];
function tryRun(label, fn) {
  try { fn(); results.push(["ok  ", label]); }
  catch (e) { results.push(["FAIL", `${label} :: ${e.message}`]); }
}

// The two unauthenticated entry points, which is where the new work lives.
tryRun("renderLanding()", () => g.renderLanding());
tryRun("renderLogin()", () => g.renderLogin());
tryRun("policy money formatting keeps SGD stable and formats INR locally", () => {
  if (g.money("60000", "SG") !== "S$60,000") throw new Error("SGD display changed");
  if (g.money("500000", "IN") !== "₹5,00,000 (5 lakh)")
    throw new Error("INR display is not local with its lakh unit");
  const precise = g.money("10000000000000001.5", "IN");
  if (precise !== "₹10,00,00,00,00,00,00,001.5 (1,00,00,00,000.00000015 crore)")
    throw new Error(`INR precision was lost: ${precise}`);
  if (g.formatPolicyDate("2026-10-19", "IN") !== "19 October 2026")
    throw new Error("policy date was not formatted");
});
tryRun("pseudo locale renders the translated shell while money stays formatted", () => {
  g.setUser({ role: "SME", name: "Demo", email: "demo@example.test", organisation_name: "Demo" });
  try {
    g.setLocale("pseudo");
    const label = g.t("nav.sme.portfolio");
    if (label === "nav.sme.portfolio") throw new Error("translation catalogue did not load");
    g.shell("SME", [{ id: "sme-portfolio", label, icon: "◈" }]);
    if (!doc._app.innerHTML.includes(label)) throw new Error("pseudo navigation did not render");
    if (!doc._app.innerHTML.includes('data-act="set-locale"')) throw new Error("language switcher missing");
    if (g.money("60000", "SG") !== "S$60,000") throw new Error("locale changed money display");
  } finally {
    g.setLocale("en");
  }
});
tryRun("planLayoutCard renders every section and preserves missing values", () => {
  const html = g.planLayoutCard({
    id: "policy-1", insurer: "Lotus General (Sandbox)", product: "Synthetic health plan",
    country: "IN", masked_number: "****-1234", status: "ACTIVE",
    period: { start: "2026-04-01", end: "2027-03-31" },
    coverage_items: [
      { id: "coverage-1", mechanism_code: "COVERED.FAMILY_FLOATER", value: null,
        evidence_class: "PROVEN", source_document_id: "doc-1" },
      { id: "coverage-2", mechanism_code: "LIMIT.SUM_INSURED", value: "500000",
        unit: "INR", evidence_class: "PROVEN", source_document_id: "doc-1" },
    ],
  });
  for (const heading of ["Covered", "Limits & sub-limits", "Waiting periods", "Your share",
    "Exclusions & conditions", "Renewal & deadlines", "Evidence"]) {
    if (!html.includes(heading.replace(/&/g, "&amp;"))) throw new Error(`missing ${heading}`);
  }
  if (!html.includes("₹5,00,000 (5 lakh)")) throw new Error("policy money was not formatted with its lakh unit");
  if (!html.includes("No value is shown")) throw new Error("missing-data state was not rendered");
  if (!html.includes('data-act="portfolio-evidence"')) throw new Error("no wording control");
  if (!html.includes('aria-label="Covered"')) throw new Error("section is not labelled");
});
tryRun("portfolio synthetic banner uses durable server provenance", () => {
  sandbox.sessionStorage.setItem("veris_household_example_loaded", "1");
  const label = g.t("portfolio.synthetic_banner");
  const html = g.portfolioSyntheticBanner({ household: {
    provenance: { kind: "SYNTHETIC_EXAMPLE", seed_id: "example" },
  } });
  if (!html.includes(label)) throw new Error("the durable synthetic label was not rendered");
  if (g.portfolioSyntheticBanner({ household: { provenance: null } }) !== "")
    throw new Error("a browser-only flag rendered a synthetic label");
  sandbox.sessionStorage.removeItem("veris_household_example_loaded");
});

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

// ── which country's law applies ────────────────────────────────────────────
// Shapes taken from the server's `brief.jurisdiction` record (jurisdiction.as_dict).
const SG_RECORD = {
  pack: "SG", pack_name: "Singapore", pack_available: true, detected: "SG",
  detected_name: "Singapore", leading: "SG", confidence: "HIGH",
  summary: 'Read as Singapore (high confidence): governing-law clause "determined in accordance with the law of Singapore" (01-policy-wording.pdf, page 6).',
  notice: null,
  reasons: [
    { direction: "for", country: "SG", country_name: "Singapore", signal: "governing_law",
      signal_label: "governing-law clause", weight: 10,
      matched: "determined in accordance with the law of Singapore",
      document: "01-policy-wording.pdf", page: 6 },
    { direction: "for", country: "SG", country_name: "Singapore", signal: "phone_numbers",
      signal_label: "phone number", weight: 1, matched: "+65 …",
      document: "02-policy-schedule.pdf", page: 1 }],
  rules_not_applied: [], sentences_dropped: [],
};
const MY_RECORD = {
  pack: "GENERIC", pack_name: "No jurisdiction pack", pack_available: false,
  detected: "MY", detected_name: "Malaysia", leading: "MY", confidence: "HIGH",
  summary: "These documents read as Malaysia. VERIS has no Malaysia jurisdiction pack yet.",
  notice: "These documents read as Malaysia. VERIS has no Malaysia jurisdiction pack yet, so it reads and quotes your documents but makes no statement about the law of <b>Malaysia</b>.",
  reasons: [
    { direction: "for", country: "MY", country_name: "Malaysia", signal: "governing_law",
      signal_label: "governing-law clause", weight: 10, matched: "governed by Malaysian Law",
      document: "wording.pdf", page: 7 },
    { direction: "against", country: "SG", country_name: "Singapore", signal: "currency",
      signal_label: "currency", weight: 3, matched: "<img src=x onerror=alert(1)>",
      document: "wording.pdf", page: 2 }],
  rules_not_applied: [{ rule_id: "WICA-HEADCOUNT-UNDERDECLARED",
    title: "Work injury declaration understated against the CPF record",
    jurisdiction: "SG", jurisdiction_name: "Singapore",
    reason: "A Singapore rule. These documents are not established as Singapore documents, so Singapore law is not applied to them." }],
  sentences_dropped: [],
};
const UNCERTAIN_GENERIC_RECORDS = [
  { name: "no-signal", pack: "GENERIC", pack_name: "No jurisdiction pack",
    pack_available: false, detected: "GENERIC", detected_name: null, leading: null,
    confidence: "NONE", summary: "Incorrect generic summary",
    notice: "VERIS could not tell which country's law governs these documents.",
    reasons: [{ direction: "against", statement: "No recognised country signal was found." }],
    rules_not_applied: [], sentences_dropped: [] },
  { name: "sub-threshold", pack: "GENERIC", pack_name: "No jurisdiction pack",
    pack_available: false, detected: "GENERIC", detected_name: null, leading: "SG",
    confidence: "LOW", summary: "Incorrect generic summary",
    notice: "The only signals were too weak to choose a jurisdiction pack.",
    reasons: [{ direction: "against", statement: "The signal was too weak." }],
    rules_not_applied: [], sentences_dropped: [] },
  { name: "tied-scores", pack: "GENERIC", pack_name: "No jurisdiction pack",
    pack_available: false, detected: "GENERIC", detected_name: null, leading: "SG",
    confidence: "LOW", summary: "Incorrect generic summary",
    notice: "The documents contain conflicting governing-law clauses.",
    reasons: [{ direction: "against", statement: "The clauses conflict." }],
    rules_not_applied: [], sentences_dropped: [] },
];
tryRun("jurisdictionPanel renders nothing without a record", () => {
  if (g.jurisdictionPanel(null) !== "" || g.jurisdictionPanel({}) !== "")
    throw new Error("a panel was drawn with no jurisdiction record");
});
tryRun("jurisdictionPanel shows the pack, the confidence and the reasons", () => {
  const html = g.jurisdictionPanel(SG_RECORD);
  if (!html.includes("Singapore pack")) throw new Error("the pack is not named");
  if (!html.includes("high confidence")) throw new Error("the confidence is not shown");
  if (!html.includes("determined in accordance with the law of Singapore"))
    throw new Error("the governing-law reason is not shown");
  if (!html.includes("page 6")) throw new Error("the reason has no page");
  if (!html.includes("<details") || !html.includes("<summary>"))
    throw new Error("the reasons are not in a keyboard-operable disclosure");
  if (!html.includes('role="region"') || !html.includes("aria-label="))
    throw new Error("the panel is not a labelled region");
  if (html.includes("Not applied")) throw new Error("an empty not-applied list was drawn");
});
tryRun("jurisdictionPanel leads with the no-pack notice and lists what was not applied", () => {
  const html = g.jurisdictionPanel(MY_RECORD);
  if (!html.includes("No jurisdiction pack")) throw new Error("the missing pack is not badged");
  if (!html.includes("no Malaysia jurisdiction pack yet")) throw new Error("the notice is missing");
  if (!html.includes("Not applied to these documents")) throw new Error("not-applied rules are hidden");
  if (!html.includes("Work injury declaration understated")) throw new Error("the rule is not named");
  if (!html.includes("Pointing elsewhere")) throw new Error("the reasons against are not shown");
});
tryRun("jurisdictionPanel keeps the fallback notice for every uncertain path", () => {
  for (const record of UNCERTAIN_GENERIC_RECORDS) {
    const html = g.jurisdictionPanel(record);
    if (!html.includes("No jurisdiction pack"))
      throw new Error(`${record.name}: the generic fallback was shown as a pack`);
    if (!html.includes(record.notice))
      throw new Error(`${record.name}: the fallback notice was not the headline`);
    if (html.includes("No jurisdiction pack pack"))
      throw new Error(`${record.name}: generic was doubled as an available pack`);
  }
  const defensive = g.jurisdictionPanel({ ...UNCERTAIN_GENERIC_RECORDS[0], pack_available: true });
  if (!defensive.includes("No jurisdiction pack") || defensive.includes("No jurisdiction pack pack"))
    throw new Error("a malformed generic response reached the reassuring pack branch");
});
tryRun("jurisdictionPanel escapes every server-derived value", () => {
  const html = g.jurisdictionPanel(MY_RECORD);
  if (html.includes("<img") || html.includes("<b>Malaysia</b>"))
    throw new Error("server text reached the DOM unescaped");
  if (!html.includes("&lt;img") || !html.includes("&lt;b&gt;Malaysia"))
    throw new Error("not escaped through esc()");
});

// ── where a statement about the law comes from (PLAN rule 17) ──────────────
const SOURCES = [
  { entry_id: "SG-COMP-WORK-INJURY", title: "Work injury compensation insurance",
    status: "HUMAN_VERIFIED", owner_facing: true,
    source_url: "https://regulator.example/wic", retrieved_on: "2026-10-10",
    quote: "You need to get <b>work injury</b> insurance.", verified_by: "VERIS team" },
  { entry_id: "SG-STAT-PDPA-PENALTY", title: "Maximum penalty", status: "SOURCE_QUOTED",
    owner_facing: true, source_url: "javascript:alert(1)", retrieved_on: "2026-10-10",
    quote: "in any other case — $1 million", verified_by: null },
  { entry_id: "SG-REG-INSURANCE", title: "Regulator", status: "CANDIDATE",
    owner_facing: false, source_url: "https://regulator.example/", retrieved_on: null,
    quote: null, verified_by: null, statement: "THIS MUST NOT RENDER" },
];
tryRun("packSources renders nothing for no sources", () => {
  if (g.packSources(undefined) !== "" || g.packSources([]) !== "")
    throw new Error("an empty source block was drawn");
});
tryRun("packSources shows each quote, status and an https link", () => {
  const html = g.packSources(SOURCES);
  if (!html.includes("HUMAN_VERIFIED") || !html.includes("SOURCE_QUOTED"))
    throw new Error("a status badge is missing");
  if (!html.includes("in any other case — $1 million")) throw new Error("a quote is missing");
  if (!html.includes('href="https://regulator.example/wic"')) throw new Error("no source link");
  if (!html.includes('rel="noopener noreferrer"')) throw new Error("the link keeps an opener");
  if (!html.includes("Retrieved")) throw new Error("the retrieval date is not shown");
});
tryRun("packSources never draws a CANDIDATE, an unsafe link or unescaped text", () => {
  const html = g.packSources(SOURCES);
  if (html.includes("THIS MUST NOT RENDER") || html.includes(">Regulator<"))
    throw new Error("a CANDIDATE entry was drawn as a source");
  if (html.includes("javascript:")) throw new Error("a script URL reached an href");
  if (html.includes("<b>work injury</b>")) throw new Error("a quote reached the DOM unescaped");
  if (!html.includes("&lt;b&gt;work injury")) throw new Error("not escaped through esc()");
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

await (async () => {
  const previousFetch = sandbox.fetch;
  const backendMessage = "This backend diagnostic must not become owner-facing copy.";
  try {
    g.setLocale("pseudo");
    const title = g.t("portfolio.load_error_title");
    const remediation = g.t("portfolio.legacy_sme_bundle_requires_reseed");
    sandbox.fetch = async path => {
      calls.push(path);
      if (path !== "/api/portfolio") throw new Error(`unexpected request: ${path}`);
      return {
        status: 409, ok: false, statusText: "Conflict",
        json: async () => ({ error: {
          code: "LEGACY_SME_BUNDLE_REQUIRES_RESEED", message: backendMessage,
        } }),
      };
    };
    const host = el("div");
    await g.smePortfolio(host);
    if (!host.innerHTML.includes('role="alert"')) throw new Error("remediation is not announced");
    if (!host.innerHTML.includes(title)) throw new Error("localized remediation title is missing");
    if (!host.innerHTML.includes(remediation)) throw new Error("localized remediation is missing");
    if (host.innerHTML.includes(backendMessage)) throw new Error("backend diagnostic reached the owner");
    if (host.innerHTML.includes("Error 409")) throw new Error("server remediation was discarded");
    results.push(["ok  ", "smePortfolio localizes the legacy demo reseed remediation"]);
  } catch (e) {
    results.push(["FAIL", `smePortfolio localizes the legacy demo reseed remediation :: ${e.message}`]);
  } finally {
    g.setLocale("en");
    sandbox.fetch = previousFetch;
  }
})();

await (async () => {
  const previousFetch = sandbox.fetch;
  try {
    sandbox.fetch = async path => {
      calls.push(path);
      if (path !== "/api/cyber") throw new Error(`unexpected request: ${path}`);
      return {
        status: 200, ok: true,
        json: async () => ({
          available: true, cover_label: "Policy exclusion located", gap_open: true,
          holdings: [{ subject: "Customers", evidence_class: "PROVEN",
            document_kind: "BUSINESS_PROFILE", basis: "An online sales channel is recorded." }],
          exclusion: { quote: "The policy excludes electronic data.", page_no: 2,
            document_kind: "BASE_WORDING", author_class: "INSURER", evidence_class: "PROVEN",
            why_it_matters: "This is the policy's own wording." },
          cost_heads: [{ head: "Restoring data", plain: "Restore records.",
            why: "The property section is for physical property.", responds: "NOTHING" }],
          uncovered_count: 1, notice: "Read from your own documents.",
        }),
      };
    };
    const host = el("div");
    await g.smeCyber(host);
    if (!host.innerHTML.includes("Cyber and data")) throw new Error("screen did not render");
    if (host.innerHTML.includes("What the law requires"))
      throw new Error("an absent statutory block rendered an empty legal card");
    if (host.innerHTML.includes("undefined")) throw new Error("missing statutory values leaked");
    results.push(["ok  ", "smeCyber omits an absent statutory block"]);
  } catch (e) {
    results.push(["FAIL", `smeCyber omits an absent statutory block :: ${e.message}`]);
  } finally {
    sandbox.fetch = previousFetch;
  }
})();

tryRun("planLayoutCard renders cited health terms with schedule operands", () => {
  const html = g.planLayoutCard({
    id: "health-policy", insurer: "Go Digit General Insurance Limited",
    product: "Arogya Sanjeevani Policy (synthetic schedule)", country: "IN",
    masked_number: "****-HEALTH6", status: "ACTIVE",
    period: { start: "2026-04-01", end: "2027-03-31" },
    coverage_items: [{ id: "schedule", mechanism_code: "COVERED.FAMILY_FLOATER",
      value: null, evidence_class: "PROVEN", source_document_id: "schedule-doc" }],
    health_mapping: { mapped: true },
    health_items: [
      { mechanism_code: "WAITING.PED", label_key: "health.item.ped_waiting",
        display: "PED_END_DATE", value: "2029-04-01", state: "DOCUMENTED",
        evidence_class: "PROVEN", member_scope: [{ name: "Lakshmi", age: "63" }],
        scope_reason_key: "health.scope.declared_condition",
        operand_citations: [{ document: "Policy schedule", page: 1,
          quote: "Lakshmi has declared diabetes." }, { document: "Policy schedule", page: 1,
          quote: "Period: 1 April 2026 to 31 March 2027\nFirst policy commencement: 1 April 2026.\nContinuous coverage before this period: 0 months.\nPortability or migration credit: 0 months.\nSum-insured enhancement: none." }],
        citation: { document: "Policy wording", page: 8,
          quote: "waiting clause", source_url: "https://publisher.example/wording" } },
      { mechanism_code: "LIMIT.ROOM_RENT", label_key: "health.item.room_rent_cap",
        display: "MONEY_PER_DAY", value: "5000", unit: "INR", params: { percent: "2" },
        state: "DOCUMENTED", evidence_class: "PROVEN", citation: { document: "Policy wording",
          page: 6, quote: "room clause", source_url: "https://publisher.example/wording" } },
      { mechanism_code: "SHARE.CO_PAYMENT", label_key: "health.item.co_payment",
        display: "PERCENT_CONDITION", value: "5", unit: "%", state: "DOCUMENTED",
        evidence_class: "PROVEN", member_scope: [{ name: "Raghavan", age: "67" },
          { name: "Lakshmi", age: "63" }], scope_reason_key: "health.scope.all_named_members",
        citation: { document: "Policy wording", page: 18,
          quote: "co-payment clause", source_url: "https://publisher.example/wording" } },
      { mechanism_code: "LIMIT.FAMILY_FLOATER_SHARED",
        label_key: "health.item.family_floater_shared", display: "CONDITION",
        state: "DOCUMENTED", evidence_class: "PROVEN",
        member_scope: [{ name: "Raghavan", age: "67" }, { name: "Lakshmi", age: "63" }],
        scope_reason_key: "health.scope.family_floater", citation: { document: "Policy schedule",
          page: 1, quote: "Synthetic family-floater sum insured: INR 500,000." } },
    ],
  });
  if (!html.includes("Declared-condition waiting period ends: 1 April 2029"))
    throw new Error("waiting-period end was not rendered");
  if (!html.includes("Room-rent cap: ₹5,000 per day (2% of the sum insured)"))
    throw new Error("room-rent cap was not rendered with its source operands");
  if (!html.includes("Co-payment condition: 5% of each admissible claim"))
    throw new Error("co-payment condition was not rendered");
  if (!html.includes("Applies to Lakshmi, age 63. The schedule records the declared condition."))
    throw new Error("the derived waiting date lost its member schedule scope");
  if (!html.includes("Applies to Raghavan, age 67, Lakshmi, age 63. The schedule identifies these insured members on the family-floater policy."))
    throw new Error("the co-payment member scope was not rendered");
  if (!html.includes("Family-floater sum insured is shared")
      || !html.includes("The schedule identifies one shared sum insured for these members."))
    throw new Error("the shared floater sum insured was not rendered");
  if (!html.includes("Schedule operands") || !html.includes("Lakshmi has declared diabetes.")
      || !html.includes("First policy commencement: 1 April 2026."))
    throw new Error("schedule citations were not rendered alongside the policy wording");
  if (!html.includes("Show the cited source") || !html.includes("publisher.example/wording"))
    throw new Error("the cited source control was not rendered");
  if (html.includes("This policy is not yet mapped"))
    throw new Error("a mapped health card still renders the generic notice");
});

tryRun("Ask VERIS renders an evidence-bound policy answer", () => {
  const html = g.askAnswerEvidence({
    outputState: "DOCUMENTED", evidenceClass: "PROVEN", requiredDocument: null,
    citations: [{ document: "Policy schedule", document_id: "doc-1", page: 1,
      quote: "Lakshmi has declared diabetes. <script>x</script>", citation_id: "citation-1",
      viewer: "PORTFOLIO" }],
  });
  if (!html.includes('data-act="ask-citation"')) throw new Error("local citation has no source action");
  if (!html.includes("Show the cited source")) throw new Error("citation control is not labelled");
  if (html.includes("<script>")) throw new Error("citation text reached the DOM unescaped");
  if (!html.includes("&lt;script&gt;")) throw new Error("citation text was not escaped");
  if (!html.includes(g.humanState("DOCUMENTED")) || !html.includes("PROVEN"))
    throw new Error("answer state and evidence class are absent");
});
tryRun("Ask VERIS preserves the navigator vocabulary for navigation requests", () => {
  for (const question of [
    "Take me to Portfolio",
    "Show me the documents screen",
    "Where are my documents?",
  ]) {
    if (!g.askNavigationIntent(question)) throw new Error(`navigation was not routed: ${question}`);
  }
  for (const question of [
    "When does Mum's waiting period end?",
    "Where does Mum's policy state the waiting-period end?",
  ]) {
    if (g.askNavigationIntent(question)) throw new Error(`policy question was sent to the navigator: ${question}`);
  }
});

await (async () => {
  const previousFetch = sandbox.fetch;
  const requests = [];
  try {
    sandbox.fetch = async path => {
      requests.push(path);
      if (path === "/api/ai/ask") {
        return { status: 200, ok: true, json: async () => ({
          text: "Cited policy answer", mode: "DETERMINISTIC", source: "DETERMINISTIC",
          citations: [], output_state: "DOCUMENTED", evidence_class: "PROVEN",
        }) };
      }
      if (path === "/api/assistant") {
        return { status: 200, ok: true, json: async () => ({
          answer: "Navigation answer", route: null, suggestions: [], mode: "DETERMINISTIC",
        }) };
      }
      throw new Error(`unexpected request: ${path}`);
    };
    await g.askSend("Where does Mum's policy state the waiting-period end?");
    await g.askSend("Show me the documents screen");
    if (requests[0] !== "/api/ai/ask")
      throw new Error(`policy wording question used ${requests[0]} instead of cited Q&A`);
    if (requests[1] !== "/api/assistant")
      throw new Error(`screen request used ${requests[1]} instead of navigation`);
    results.push(["ok  ", "Ask VERIS dispatches policy wording questions to cited Q&A"]);
  } catch (e) {
    results.push(["FAIL", `Ask VERIS dispatches policy wording questions to cited Q&A :: ${e.message}`]);
  } finally {
    sandbox.fetch = previousFetch;
  }
})();

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
