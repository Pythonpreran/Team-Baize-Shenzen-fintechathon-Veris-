/* Throwaway harness: execute app.js against a minimal DOM stub and drive the render paths.
   Catches reference errors and typos that a static `node --check` cannot. Deleted after use. */
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ENGLISH_CATALOGUE = JSON.parse(readFileSync("web/i18n/en.json", "utf8"));
const CHINESE_CATALOGUE = JSON.parse(readFileSync("web/i18n/zh-CN.json", "utf8"));

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
    if (path === "/static/i18n/zh-CN.json") {
      return { status: 200, ok: true, json: async () => CHINESE_CATALOGUE };
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
tryRun("planLayoutCard renders home and travel terms, the contents gap and withheld plan limits", () => {
  const html = g.planLayoutCard({
    id: "policy-home", insurer: "AIG Asia Pacific Insurance Pte. Ltd.",
    product: "AIG Homes Advantage (synthetic schedule)", country: "SG", masked_number: "****-HOME3",
    status: "ACTIVE", period: { start: "2026-02-01", end: "2027-01-31" }, coverage_items: [],
    property_travel_mapping: { mapped: true, notice_key: null },
    property_travel_items: [
      { mechanism_code: "LIMIT.CONTENTS_BELOW_LIST", label_key: "home.item.contents_below_list",
        display: "MONEY_GAP", state: "DOCUMENTED", evidence_class: "DECLARED", value: "120000", unit: "SGD",
        params: { sum_insured: "300000", contents_list: "420000", proportion_percent: "71.43" },
        notice_key: "home.notice.contents_illustration",
        citation: { document: "Homes Advantage", page: 6, quote: "proportion of the sum insured" } },
      { mechanism_code: "CONDITION.UNOCCUPANCY", label_key: "home.item.unoccupancy",
        display: "DAYS_CONDITION", state: "DOCUMENTED", evidence_class: "PROVEN", value: "60", unit: "days",
        citation: { document: "Homes Advantage", page: 1, quote: "unoccupied for more than 60 consecutive days" } },
      { mechanism_code: "LIMIT.TRIP_CANCELLATION", label_key: "travel.item.cancellation_limit",
        display: "PLAN_LIMIT", state: "INFORMATION_MISSING", evidence_class: "UNESTABLISHED", value: null,
        missing_document_key: "travel.document.selected_plan",
        citation: { document: "Travel Guard", page: 14, quote: "for the plan which you have selected" } },
    ],
  });
  if (!html.includes("S$120,000") || !html.includes("S$420,000") || !html.includes("71.43%"))
    throw new Error("the contents gap was not rendered with both operands");
  if (!html.includes(vm.runInContext("esc(t('home.notice.contents_illustration'))", ctx)))
    throw new Error("the illustration notice is missing");
  if (!html.includes(vm.runInContext("esc(t('travel.document.selected_plan'))", ctx)))
    throw new Error("a withheld plan limit does not name the document that would establish it");
  if (html.includes(vm.runInContext("esc(t('portfolio.generic_notice', { country: 'SG' }))", ctx)))
    throw new Error("a mapped home card was labelled as not yet mapped");
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
// ── our own document: the page itself, with the cited span ringed ──────────
// The other side of the rights boundary. A page image is drawn ONLY for a document the
// reader uploaded or one of our synthetic samples, and this stub is the shape the server
// sends for those: an image plus the highlight's bounding box as fractions of it.
const pagePayload = (highlight) => ({
  display: "PAGE", available: true, filename: "02-policy-schedule.pdf",
  page_no: 17, page_count: 28, image: "/api/source/doc-1/page/17.png?span_id=s1",
  highlight,
});
tryRun("drawerPagePane draws the page, rings the cited box and offers full screen", () => {
  const html = g.drawerPagePane({
    finding: { spans: [{ document_id: "doc-1", page_no: 17, span_id: "s1" }] },
    spanIndex: 0,
    page: pagePayload({ found: true, method: "EXACT", rect_count: 1,
      bounds: { x: 0.12, y: 0.63, w: 0.74, h: 0.045 } }) });
  if (!html.includes("<img src=")) throw new Error("no page image for our own document");
  if (!html.includes("pv__hlbox")) throw new Error("the cited span is not ringed");
  if (!html.includes("top:63.000%")) throw new Error("the highlight box is not placed from its fractions");
  if (!html.includes('data-act="pagefull-open"')) throw new Error("no full-screen control");
  if (!html.includes("aria-label=")) throw new Error("the full-screen control is unnamed");
  if (!html.includes("Page 17 of 28")) throw new Error("the page citation is missing");
  if (html.includes("pv__rights")) throw new Error("a rights boundary was claimed for our own page");
});
tryRun("drawerPagePane draws no ring when the quotation was not located", () => {
  const html = g.drawerPagePane({
    finding: { spans: [{ document_id: "doc-1", page_no: 17, span_id: "s1" }] },
    spanIndex: 0,
    page: pagePayload({ found: false, method: "NOT_FOUND", rect_count: 0, bounds: null,
      note: "The quotation could not be located on this page." }) });
  if (html.includes("pv__hlbox")) throw new Error("a box was invented for a quotation nobody found");
  if (!html.includes("could not be located")) throw new Error("the miss is not stated");
  if (!html.includes("<img src=")) throw new Error("the page itself should still be shown");
});
tryRun("drawerPagePane clamps or refuses a hostile highlight box", () => {
  const ring = (bounds) => g.drawerPagePane({
    finding: { spans: [{ document_id: "doc-1", page_no: 17, span_id: "s1" }] },
    spanIndex: 0,
    page: pagePayload({ found: true, method: "PARTIAL", rect_count: 2, bounds }) });
  // A width that is not a number is not a box, so no ring is drawn and nothing from the
  // payload reaches the style attribute.
  const hostile = ring({ x: 0.1, y: 0.2, w: "12\" onmouseover=alert(1) x=\"", h: 0.1 });
  if (hostile.includes("onmouseover")) throw new Error("a payload escaped the style attribute");
  if (hostile.includes("pv__hlbox")) throw new Error("a ring was drawn for a box with no width");
  // Numbers outside the image are clamped to its edges rather than painting outside it.
  const wild = ring({ x: -4, y: 99, w: 50, h: 50 });
  if (!wild.includes("left:0.000%") || !wild.includes("top:100.000%"))
    throw new Error("out-of-range fractions were not clamped");
});
tryRun("drawerPagePane routes an EXCERPT payload to the paragraph view", () => {
  const html = g.drawerPagePane({
    finding: { spans: [{ document_id: "doc-1", page_no: 15, span_id: "s1" }] },
    spanIndex: 0,
    page: excerptPayload({ available: true, url: "https://publisher.example/w.pdf" }) });
  // The heading used to read "The paragraph it came from". It now names the RIGHTS
  // BOUNDARY the branch exists for — a published wording, cited paragraph only — and
  // comes from the catalogue rather than a literal. Same assertion, current copy.
  if (!html.includes("cited paragraph only")) throw new Error("wrong heading");
  if (!html.includes("<mark>")) throw new Error("no marked quotation");
  if (html.includes("<img") || html.includes("pagefull-open"))
    throw new Error("a page image or full-screen control was drawn");
  // Still true after the rebuild, and the whole point of the branch: the page count is
  // stated, the publisher is named, and nothing offers a page view.
  if (!html.includes("Page 15 of")) throw new Error("the page citation is not prominent");
  if (!html.includes("pv__rights")) throw new Error("the rights boundary is not stated");
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
// T27: Malaysia now has a pack, so the recognised-without-pack example is Australia.
const NO_PACK_RECORD = {
  pack: "GENERIC", pack_name: "No jurisdiction pack", pack_available: false,
  detected: "AU", detected_name: "Australia", leading: "AU", confidence: "HIGH",
  summary: "These documents read as Australia. VERIS has no Australia jurisdiction pack yet.",
  notice: "These documents read as Australia. VERIS has no Australia jurisdiction pack yet, so it reads and quotes your documents but makes no statement about the law of <b>Australia</b>.",
  reasons: [
    { direction: "for", country: "AU", country_name: "Australia", signal: "governing_law",
      signal_label: "governing-law clause", weight: 10,
      matched: "governed by the laws of New South Wales", document: "wording.pdf", page: 7 },
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
  const html = g.jurisdictionPanel(NO_PACK_RECORD);
  if (!html.includes("No jurisdiction pack")) throw new Error("the missing pack is not badged");
  if (!html.includes("no Australia jurisdiction pack yet")) throw new Error("the notice is missing");
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
  const html = g.jurisdictionPanel(NO_PACK_RECORD);
  if (html.includes("<img") || html.includes("<b>Australia</b>"))
    throw new Error("server text reached the DOM unescaped");
  if (!html.includes("&lt;img") || !html.includes("&lt;b&gt;Australia"))
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
  // This used to assert the RAW CODES `HUMAN_VERIFIED` / `SOURCE_QUOTED` appeared in the
  // markup. That pill — a 10.5px uppercase engine code in a pale tint — was the defect:
  // illegible, and meaningless to the reader even when legible. The status is now a
  // sentence with a glyph, so the equivalent assertion is that each status still reaches
  // the reader, named rather than coded.
  if (!html.includes("Confirmed by a named reviewer")
    || !html.includes("Quoted from the primary source"))
    throw new Error("a status badge is missing");
  if (!html.includes("src-badge__g")) throw new Error("the status has no non-colour marker");
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
tryRun("Ask VERIS preserves navigator commands without hiding policy questions", () => {
  vm.runInContext(`ASK.dests = [
    { id: "sme-docs-hub", label: "Documents", trail: ["Portfolio", "Documents"] },
    { id: "sme-claims", label: "Claims", trail: ["Claims"] },
  ];`, ctx);
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
    "Where is Mum's claim deadline stated?",
    "Show me the documents I need for Mum's claim",
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
    await g.askSend("Where is Mum's claim deadline stated?");
    await g.askSend("Show me the documents I need for Mum's claim");
    await g.askSend("Show me the documents screen");
    if (requests[0] !== "/api/ai/ask")
      throw new Error(`policy wording question used ${requests[0]} instead of cited Q&A`);
    if (requests[1] !== "/api/ai/ask")
      throw new Error(`claim deadline question used ${requests[1]} instead of cited Q&A`);
    if (requests[2] !== "/api/ai/ask")
      throw new Error(`documents-needed question used ${requests[2]} instead of cited Q&A`);
    if (requests[3] !== "/api/assistant")
      throw new Error(`screen request used ${requests[3]} instead of navigation`);
    results.push(["ok  ", "Ask VERIS dispatches policy questions containing destination nouns to cited Q&A"]);
  } catch (e) {
    results.push(["FAIL", `Ask VERIS dispatches policy wording questions to cited Q&A :: ${e.message}`]);
  } finally {
    sandbox.fetch = previousFetch;
  }
})();

await (async () => {
  const previousFetch = sandbox.fetch;
  const requests = [];
  let resolveCatalogue;
  try {
    vm.runInContext(`ASK.dests = []; ASK.destinationStatus = "idle"; ASK.destinationLoad = null;
      ASK.msgs = []; ASK.busy = false;`, ctx);
    sandbox.fetch = path => {
      requests.push(path);
      if (path === "/api/assistant/destinations") {
        return new Promise(resolve => { resolveCatalogue = resolve; });
      }
      if (path === "/api/assistant") {
        return Promise.resolve({ status: 200, ok: true, json: async () => ({
          answer: "Navigation answer", route: null, suggestions: [], mode: "DETERMINISTIC",
        }) });
      }
      throw new Error(`unexpected request: ${path}`);
    };
    const sending = g.askSend("Show me Documents");
    await Promise.resolve();
    if (requests.join(",") !== "/api/assistant/destinations")
      throw new Error("an unloaded catalogue did not block classification");
    resolveCatalogue({ status: 200, ok: true, json: async () => ({
      destinations: [{ id: "sme-docs-hub", label: "Documents", trail: ["Portfolio", "Documents"] }],
    }) });
    await sending;
    if (requests[1] !== "/api/assistant")
      throw new Error("an exact destination did not wait for the catalogue then navigate");

    vm.runInContext(`ASK.dests = []; ASK.destinationStatus = "idle"; ASK.destinationLoad = null;
      ASK.msgs = []; ASK.busy = false;`, ctx);
    requests.length = 0;
    sandbox.fetch = async path => {
      requests.push(path);
      if (path === "/api/assistant/destinations") {
        return { status: 503, ok: false, json: async () => ({ detail: "unavailable" }) };
      }
      throw new Error(`unexpected request after catalogue failure: ${path}`);
    };
    await g.askSend("Show me Documents");
    if (requests.length !== 1 || requests[0] !== "/api/assistant/destinations")
      throw new Error("a failed catalogue sent an ambiguous destination elsewhere");
    const failureText = vm.runInContext(`ASK.msgs.at(-1).text`, ctx);
    if (failureText !== g.t("ask.destination_unavailable"))
      throw new Error("a failed catalogue did not explain the safe retry path");
    results.push(["ok  ", "Ask VERIS waits for its destination catalogue and fails closed on catalogue errors"]);
  } catch (e) {
    results.push(["FAIL", `Ask VERIS catalogue routing safety :: ${e.message}`]);
  } finally {
    sandbox.fetch = previousFetch;
  }
})();

await (async () => {
  const previousFetch = sandbox.fetch;
  try {
    const manifest = "a".repeat(64);
    sandbox.fetch = async path => {
      const payloads = {
        "/api/portfolio": { policies: [{ id: "health-1", line_of_business: "HEALTH", source_document_ids: ["doc-1"] }] },
        "/api/claims": { claims: [] },
        "/api/agent/status": { consent: { granted: false } },
        "/api/agent/confirmations": { confirmations: [{
          id: "pending-preview", tool_name: "claim.submit", summary: "Synthetic preview", payload_hash: "h".repeat(64),
          preview: {
            tenant_id: "org-hocklee", tenant_scoped: true, payload_hash: "h".repeat(64),
            canonical_payload: `{"claim_id":"claim-preview","packet_manifest_sha256":"${manifest}"}`,
            fields: [
              { name: "claim_id", value: "claim-preview" },
              { name: "packet_manifest_sha256", value: manifest },
            ],
          },
        }] },
        "/api/protection-map": { cells: [], overlaps: [], next_document: { document: "school policy", reason: "needed" } },
        "/api/protection-map/events": { events: [{ code: "NEW_BABY", label: "New baby" }] },
      };
      if (!(path in payloads)) throw new Error(`unexpected demo-spine request: ${path}`);
      return { status: 200, ok: true, json: async () => payloads[path] };
    };
    await g.smeClaims(doc._app);
    if (!doc._app.innerHTML.includes('data-act="claim-start"')
        || !doc._app.innerHTML.includes("Agent trace")
        || !doc._app.innerHTML.includes("Review exact action before step-up")
        || !doc._app.innerHTML.includes("claim-preview")
        || !doc._app.innerHTML.includes(manifest)
        || !doc._app.innerHTML.includes('data-act="agent-confirm"'))
      throw new Error("claims demo spine did not render the exact confirmation preview before step-up");
    await g.smeProtectionMap(doc._app);
    if (!doc._app.innerHTML.includes('data-act="life-event"')
        || !doc._app.innerHTML.includes("Protection map"))
      throw new Error("protection map did not render life-event controls");
    results.push(["ok  ", "Connected demo spine renders claims, agent and protection-map controls"]);
  } catch (e) {
    results.push(["FAIL", `Connected demo spine controls :: ${e.message}`]);
  } finally {
    sandbox.fetch = previousFetch;
  }
})();

tryRun("connected demo controls keep insurer events separate and require owner-entered scenarios", () => {
  for (const forbidden of ["claim-sandbox-query", "claim-sandbox-decision", 'loss: "50000"', 'repair_cost: "10000"']) {
    if (code.includes(forbidden)) throw new Error(`unsafe synthetic owner control remains: ${forbidden}`);
  }
  for (const required of ["agent-mandate-revoke", "claim-document-kind", "settlement-loss", "protection-scenario-loss"]) {
    if (!code.includes(required)) throw new Error(`missing reviewed control: ${required}`);
  }
});

/* ── Interaction polish: the shell worker, photo capture, the household tour, demo reset ── */

// The worker runs in its own global scope. This stub records which requests it answers and
// what it stores, so the rules "never /api/, never a navigation, only the shell list" are
// exercised as behaviour rather than read as source text.
async function runWorker() {
  const handlers = {};
  const stored = [];
  const cache = {
    put: async (key, _response) => { stored.push(String(key)); },
    match: async () => undefined,
    addAll: async list => { list.forEach(item => stored.push(`precache:${item}`)); },
  };
  const scope = {
    location: { origin: "https://veris.test" },
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: async () => {}, clients: { claim: async () => {} },
  };
  const workerContext = vm.createContext({
    self: scope, URL, console, Promise, Set, Object, String,
    caches: { open: async () => cache, keys: async () => ["veris-shell-v0", "other-cache"],
      delete: async key => { stored.push(`delete:${key}`); return true; }, match: async () => undefined },
    fetch: async () => ({ ok: true, status: 200, type: "basic", redirected: false, clone() { return this; } }),
  });
  vm.runInContext(readFileSync("web/sw.js", "utf8"), workerContext, { filename: "sw.js" });
  const answered = [];
  async function request(path, { method = "GET", mode = "cors" } = {}) {
    let promise = null;
    handlers.fetch({ request: { url: `https://veris.test${path}`, method, mode },
      respondWith: value => { promise = value; } });
    answered.push([path, method, mode, Boolean(promise)]);
    if (promise) await promise;
    return Boolean(promise);
  }
  let installed = null;
  handlers.install({ waitUntil: value => { installed = value; } });
  await installed;
  return { handlers, stored, request, answered };
}

const worker = await runWorker();
for (const [path, options, expected] of [
  ["/api/portfolio", {}, false],
  ["/api/claims/claim-1", {}, false],
  ["/static/app.js", { method: "POST" }, false],
  ["/static/app.js?v=2", {}, false],
  ["/static/landing/landing.js", {}, false],
  ["/static/app.js", {}, true],
  ["/app", { mode: "navigate" }, true],
]) {
  const answered = await worker.request(path, options);
  tryRun(`service worker ${expected ? "answers" : "ignores"} ${options.method || "GET"} ${path}${options.mode ? ` (${options.mode})` : ""}`, () => {
    if (answered !== expected) throw new Error(`expected respondWith=${expected}, got ${answered}`);
  });
}
tryRun("service worker stores only the public shell list, never a navigation or API body", () => {
  const writes = worker.stored.filter(item => !item.startsWith("precache:") && !item.startsWith("delete:"));
  if (writes.some(item => item.startsWith("/api/") || item === "/app")) throw new Error(`stored ${writes.join(", ")}`);
  if (!writes.includes("/static/app.js")) throw new Error("the shell script was not refreshed into the cache");
  const precached = worker.stored.filter(item => item.startsWith("precache:")).map(item => item.slice(9));
  if (!precached.length || precached.some(item => !item.startsWith("/static/")))
    throw new Error(`precache reaches outside /static/: ${precached.join(", ")}`);
});
tryRun("app.js registers the worker only where the browser has one", () => {
  if (typeof g.registerShellWorker !== "function") throw new Error("registerShellWorker missing");
  g.registerShellWorker();   // navigator here has no serviceWorker: must be a silent no-op
});
tryRun("signing out clears the shell caches without a worker present", () => {
  const pending = g.clearShellCaches();
  if (!pending || typeof pending.then !== "function") throw new Error("cache clearing is not awaitable");
  if (!code.includes("await clearShellCaches();")) throw new Error("logout does not wait for the caches to clear");
});
tryRun("jpegPdf wraps a photo as one valid PDF page with a real xref table", () => {
  const jpeg = Uint8Array.of(0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0xff, 0xd9);
  const pdf = g.jpegPdf(jpeg, 640, 480);
  const text = Array.from(pdf, b => String.fromCharCode(b)).join("");
  if (!text.startsWith("%PDF-1.4\n")) throw new Error("not a PDF header");
  if (!text.includes("/Filter /DCTDecode") || !text.includes("/Width 640 /Height 480"))
    throw new Error("image XObject missing");
  // 640 x 480 inside a landscape A4 outline (842 x 595 pt): height binds, 793 x 595.
  if (!text.includes("/MediaBox [0 0 793 595]")) throw new Error("landscape page box not proportional");
  const xrefAt = Number(/startxref\n(\d+)\n%%EOF\n$/.exec(text)?.[1]);
  if (text.slice(xrefAt, xrefAt + 4) !== "xref") throw new Error("startxref does not point at the table");
  const offsets = [...text.slice(xrefAt).matchAll(/(\d{10}) 00000 n /g)].map(m => Number(m[1]));
  offsets.forEach((offset, index) => {
    if (!text.startsWith(`${index + 1} 0 obj`, offset)) throw new Error(`object ${index + 1} offset is wrong`);
  });
  const imageAt = text.indexOf("stream\n", text.indexOf("/DCTDecode")) + 7;
  if (pdf[imageAt] !== 0xff || pdf[imageAt + 1] !== 0xd8) throw new Error("JPEG bytes were not embedded verbatim");
});
tryRun("household tour steps come from the catalogue and point at real screens", () => {
  const steps = g.householdTourSteps();
  if (steps.length < 6) throw new Error("too few household steps");
  for (const step of steps) {
    if (!step.title || step.title.startsWith("tour.") || !step.body || step.body.startsWith("tour."))
      throw new Error(`untranslated step ${step.title}`);
    if (step.view && !String(step.view).startsWith("sme-")) throw new Error(`foreign view ${step.view}`);
  }
});
tryRun("demo reset is two steps and the second names what is kept", () => {
  vm.runInContext("S.demoResetConfirm = false", ctx);
  const first = g.demoResetCard();
  if (!first.includes('data-act="demo-reset"') || first.includes("demo-reset-confirm"))
    throw new Error("the first step must not offer the destructive action");
  vm.runInContext("S.demoResetConfirm = true", ctx);
  const second = g.demoResetCard();
  if (!second.includes('data-act="demo-reset-confirm"') || !second.includes('role="alertdialog"'))
    throw new Error("the confirmation step is not an alert dialog");
  if (!second.includes(vm.runInContext("esc(t('demo_reset.confirm_body'))", ctx)))
    throw new Error("the kept-material notice is missing");
  vm.runInContext("S.demoResetConfirm = false", ctx);
});
tryRun("claim photos are checked against the same 2 MB and type limits as the server", () => {
  if (g.claimFileProblem({ name: "a.jpg", size: 2_000_001, type: "image/jpeg" }) === "")
    throw new Error("an oversized photo was accepted");
  if (g.claimFileProblem({ name: "a.heic", size: 1000, type: "image/heic" }) === "")
    throw new Error("an unsupported type was accepted");
  if (g.claimFileProblem({ name: "a.jpg", size: 1000, type: "image/jpeg" }) !== "")
    throw new Error("a valid photo was refused");
});
tryRun("the shell offers a skip link and a phone navigation bar", () => {
  g.setUser({ role: "SME", name: "Demo", email: "demo@example.test", organisation_name: "Demo" });
  g.shell("SME", [{ id: "sme-portfolio", label: "Portfolio", icon: "◈" }]);
  const html = doc._app.innerHTML;
  if (!html.includes('data-act="skip-main"')) throw new Error("no skip link");
  if (!html.includes('class="mobile-nav"')) throw new Error("no small-screen navigation");
  if (!html.includes('id="main" tabindex="-1"')) throw new Error("main cannot take focus");
});

/* Optional: write the PDF jpegPdf makes from a real JPEG, so the Python suite can send it
   through the actual upload pipeline. `node tools/smoke_frontend.mjs --photo-pdf in out w h` */
const photoArg = process.argv.indexOf("--photo-pdf");
if (photoArg >= 0) {
  const [input, output, width, height] = process.argv.slice(photoArg + 1, photoArg + 5);
  const { writeFileSync } = await import("node:fs");
  writeFileSync(output, g.jpegPdf(new Uint8Array(readFileSync(input)), Number(width), Number(height)));
}

/* T28: Simplified Chinese. The catalogue arrives asynchronously, so this check awaits
   setLocale; English stays on screen until it does and for every English-only key. */
async function tryRunAsync(label, fn) {
  try { await fn(); results.push(["ok  ", label]); }
  catch (e) { results.push(["FAIL", `${label} :: ${e.message}`]); }
}
await tryRunAsync("zh-CN translates owner screens, keeps money per policy country and marks English-only screens", async () => {
  g.setUser({ role: "SME", name: "Demo", email: "demo@example.test", organisation_name: "Demo" });
  try {
    await g.setLocale("zh-CN");
    const label = g.t("nav.sme.portfolio");
    if (label !== CHINESE_CATALOGUE.nav.sme.portfolio) throw new Error(`nav label was ${label}`);
    if (doc.documentElement.lang !== "zh-CN") throw new Error("html lang was not zh-CN");
    g.shell("SME", [{ id: "sme-portfolio", label, icon: "◈" }]);
    if (!doc._app.innerHTML.includes(label)) throw new Error("Chinese navigation did not render");
    if (!doc._app.innerHTML.includes('value="zh-CN"')) throw new Error("Chinese option missing");
    if (g.money("60000", "SG") !== "S$60,000") throw new Error("UI locale changed SGD display");
    if (g.money("500000", "CN") !== "¥500,000") throw new Error("CNY display is wrong");
    if (g.t("reviewer.title") !== ENGLISH_CATALOGUE.reviewer.title)
      throw new Error("an English-only reviewer key was not left in English");
    const notice = g.localeNotice("ADVISER");
    if (!notice.includes(CHINESE_CATALOGUE.locale.english_only))
      throw new Error("adviser screens do not say they are English-only");
  } finally {
    await g.setLocale("en");
  }
  if (g.t("nav.sme.portfolio") !== ENGLISH_CATALOGUE.nav.sme.portfolio)
    throw new Error("switching back did not restore English");
});

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
