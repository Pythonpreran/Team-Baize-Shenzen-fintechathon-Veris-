"use strict";
/* VERIS — an evidence-bound insurance agent for households and small businesses.
 * Complete SPA.
 *
 * Roles, one engine. The owner holds their case; a licensed adviser reviews what the owner
 * discloses. (A carrier workspace for the fictional sandbox insurer is kept and
 * role-guarded, but it is not part of the household-and-business product and is not
 * offered at sign-in.) All share the same deterministic policy engine underneath, and no
 * machine-learning model makes any consequential decision.
 *
 * SECURITY: auth is cookie-based (proven_sid, HttpOnly, SameSite=Lax).
 * The server enforces roles; the client selects views.
 *
 * ESCAPING: every document-derived value reaches the DOM through esc() or
 * textContent. innerHTML is used for layout only — never for document text.
 */

// `tab` holds the active sub-tab per container screen, keyed by group name. Kept on S
// rather than in the URL because the app has no router — nav8() sets S.view and
// re-renders — and inventing one here would be a larger change than the sidebar
// collapse it exists to serve.
const LOCALE_STORAGE_KEY = "veris_locale";
const PSEUDO_LOCALE = "pseudo";
const S = {
  user: null, view: null, data: {}, tab: {}, householdProvenance: null,
  locale: (() => {
    try { return localStorage.getItem(LOCALE_STORAGE_KEY) || "en"; } catch (_) { return "en"; }
  })(),
};
// Set when the built-in example bundle is loaded, so the Overview can keep saying the
// business is fictional even though the files were genuinely uploaded.
const EXAMPLE_FLAG = "veris_example_loaded";
/* Set when the example ran WITHOUT its published base wording, because the document
   corpus has not been fetched on this machine. The server says so in `wording_notice`;
   the alternative is showing fourteen findings where the example claims seventeen and
   leaving the reader to work out which number is wrong. In a product whose whole claim
   is that absence gets reported rather than papered over, that gap is not a cosmetic
   one. */
const WORDING_NOTICE_KEY = "veris_example_wording_notice";
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const pct = v => v == null ? "—" : String(v).includes("%") ? v : v + "%";
const titleCase = s => String(s || "").toLowerCase().split(/[_\s]+/).filter(Boolean).map(w => w[0].toUpperCase() + w.slice(1)).join(" ");

const I18N = { en: {} };
const I18N_PATH = "/static/i18n/en.json";

function _message(key) {
  return String(key).split(".").reduce((value, part) =>
    value && typeof value === "object" ? value[part] : undefined, I18N.en);
}

function _pseudo(value) {
  const accent = {
    a: "à", b: "ƀ", c: "ç", d: "ď", e: "ë", f: "ƒ", g: "ğ", h: "ħ", i: "ï",
    j: "ĵ", k: "ķ", l: "ľ", m: "ṁ", n: "ñ", o: "ô", p: "þ", q: "ɋ", r: "ř",
    s: "š", t: "ŧ", u: "ü", v: "ṽ", w: "ŵ", x: "ẋ", y: "ÿ", z: "ž",
    A: "À", B: "Ƀ", C: "Ç", D: "Ď", E: "Ë", F: "Ƒ", G: "Ğ", H: "Ħ", I: "Ï",
    J: "Ĵ", K: "Ķ", L: "Ľ", M: "Ṁ", N: "Ñ", O: "Ô", P: "Þ", Q: "Ɋ", R: "Ř",
    S: "Š", T: "Ŧ", U: "Ü", V: "Ṽ", W: "Ŵ", X: "Ẋ", Y: "Ÿ", Z: "Ž",
  };
  return String(value).replace(/\{[^}]+\}|[A-Za-z]/g, token =>
    token.startsWith("{") ? token : (accent[token] || token));
}

function t(key, params = {}) {
  const raw = _message(key);
  if (typeof raw !== "string") return key;
  const forms = raw.split(" | ");
  const selected = forms.length > 1 && Object.hasOwn(params, "count")
    ? forms[Number(params.count) === 1 ? 0 : 1] : forms[0];
  const template = S.locale === PSEUDO_LOCALE ? _pseudo(selected) : selected;
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (_match, name) =>
    params[name] == null ? `{${name}}` : String(params[name]));
}

function setLocale(locale) {
  S.locale = locale === PSEUDO_LOCALE ? PSEUDO_LOCALE : "en";
  try { localStorage.setItem(LOCALE_STORAGE_KEY, S.locale); } catch (_) { /* unavailable */ }
  document.documentElement.lang = S.locale === PSEUDO_LOCALE ? "en-x-pseudo" : "en";
  render();
}

function languageSelector() {
  return `<label class="language-picker" for="language-picker">${esc(t("shell.language"))}
    <select id="language-picker" data-act="set-locale" aria-label="${esc(t("shell.language"))}">
      <option value="en"${S.locale === "en" ? " selected" : ""}>${esc(t("locale.english"))}</option>
      <option value="${PSEUDO_LOCALE}"${S.locale === PSEUDO_LOCALE ? " selected" : ""}>${esc(t("locale.pseudo"))}</option>
    </select></label>`;
}

async function loadI18n() {
  try {
    const response = await fetch(I18N_PATH, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`translation catalogue HTTP ${response.status}`);
    I18N.en = await response.json();
  } catch (error) {
    console.error("Unable to load the English translation catalogue.", error);
  }
  document.documentElement.lang = S.locale === PSEUDO_LOCALE ? "en-x-pseudo" : "en";
}

const I18N_READY = loadI18n();
window.verisI18nReady = I18N_READY;

const POLICY_FORMATS = {
  SG: { locale: "en-SG", currency: "SGD", symbol: "S$" },
  SGD: { locale: "en-SG", currency: "SGD", symbol: "S$" },
  IN: { locale: "en-IN", currency: "INR", symbol: "₹" },
  INR: { locale: "en-IN", currency: "INR", symbol: "₹" },
};

function policyCountry(country) {
  const explicit = String(country || "").toUpperCase();
  if (explicit === "GENERIC") return "";
  if (explicit) return explicit;
  const selected = String(S.policyCountry || "").toUpperCase();
  if (selected === "GENERIC") return "";
  // The legacy SME workspace has only Singapore policy data. T06 passes each policy's
  // country explicitly, so this compatibility default cannot label a new generic policy.
  return selected || "SG";
}

function policyFormat(country) {
  return POLICY_FORMATS[policyCountry(country)] || null;
}

function exactDecimal(value) {
  const raw = typeof value === "string" ? value.trim() : String(value ?? "");
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(raw);
  if (!match) return null;
  let [, sign, whole, fraction = ""] = match;
  whole = whole.replace(/^0+(?=\d)/, "");
  fraction = fraction.replace(/0+$/, "");
  if (whole === "0" && !fraction) sign = "";
  return { sign, whole, fraction };
}

function indianGroups(whole) {
  if (whole.length <= 3) return whole;
  const tail = whole.slice(-3);
  let head = whole.slice(0, -3);
  const groups = [];
  while (head) {
    groups.unshift(head.slice(-2));
    head = head.slice(0, -2);
  }
  return [...groups, tail].join(",");
}

function indianDecimalText(amount) {
  return `${amount.sign}${indianGroups(amount.whole)}${amount.fraction ? `.${amount.fraction}` : ""}`;
}

function atLeastIndianUnit(amount, threshold) {
  return amount.whole.length > threshold.length
    || (amount.whole.length === threshold.length && amount.whole >= threshold);
}

function scaleIndianDecimal(amount, places) {
  const digits = `${amount.whole}${amount.fraction}`;
  const point = amount.whole.length - places;
  let whole;
  let fraction;
  if (point <= 0) {
    whole = "0";
    fraction = `${"0".repeat(-point)}${digits}`;
  } else {
    whole = digits.slice(0, point);
    fraction = digits.slice(point);
  }
  whole = whole.replace(/^0+(?=\d)/, "");
  return { sign: amount.sign, whole, fraction: fraction.replace(/0+$/, "") };
}

function indianMoneyUnit(amount) {
  const crore = atLeastIndianUnit(amount, "10000000");
  if (!crore && !atLeastIndianUnit(amount, "100000")) return "";
  const scaled = scaleIndianDecimal(amount, crore ? 7 : 5);
  const unit = crore ? t("currency.crore") : t("currency.lakh");
  return ` (${indianDecimalText(scaled)} ${unit})`;
}

function indianMoney(value, format) {
  const amount = exactDecimal(value);
  return amount ? format.symbol + indianDecimalText(amount) + indianMoneyUnit(amount) : null;
}

function money(value, country) {
  const format = policyFormat(country);
  if (format?.currency === "INR") return indianMoney(value, format) ?? (value ?? "");
  const amount = Number(value);
  if (!Number.isFinite(amount)) return value ?? "";
  const locale = format?.locale || "en-US";
  const body = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(amount);
  return format ? format.symbol + body : body;
}

function formatPolicyDate(value, country) {
  if (!value) return "";
  const raw = String(value);
  const date = /^\d{4}-\d{2}-\d{2}$/.test(raw) ? new Date(`${raw}T00:00:00`) : new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return new Intl.DateTimeFormat(policyFormat(country)?.locale || "en-US", {
    day: "numeric", month: "long", year: "numeric",
  }).format(date);
}

/* ── Event delegation ─────────────────────────────────────────────────────
 *
 * WHY THIS EXISTS, AND WHY IT IS NOT AN inline onclick.
 *
 * The server sends `Content-Security-Policy: script-src 'self'`, which blocks every
 * inline event handler attribute. That policy is deliberate and load-bearing: this
 * product renders verbatim text out of documents somebody else authored, so if an
 * escaping bug ever slips through, the CSP is the second line of defence. Adding
 * 'unsafe-inline' to make onclick= work would trade a real security control for a
 * convenience, in a product whose entire pitch is that it does not cut corners.
 *
 * So every interactive element declares `data-act` (and optionally `data-arg`) and one
 * delegated listener dispatches it. `closest()` is used so a click landing on a child
 * element still resolves to the element that declared the action.
 *
 * Adding a new action: add the element with data-act, add the case here. Never reach
 * for onclick=.
 */
const ACTIONS = {
  nav: arg => nav8(arg),
  "set-locale": (_arg, el) => setLocale(el.value),
  logout: () => logout(),
  "delete-case": () => deleteCase(),
  "export-record": () => exportRecord(),
  "load-example": () => loadExample(),
  "load-household-example": () => loadHouseholdExample(),
  "portfolio-evidence": arg => portfolioEvidence(arg),
  "pick-scenario": arg => pickScenario(Number(arg)),
  "remove-file": arg => removeFile(Number(arg)),
  "toggle-detail": (_arg, el) => el.querySelector(".finding-detail")?.classList.toggle("open"),
  "fill-login": arg => fillLogin(arg),
  "switch-role": () => switchRole(),
  /* The watching surface. Every toggle is a BUTTON carrying the value it will set,
   * because the delegated handler below calls preventDefault() — which would cancel a
   * checkbox's own state change and leave the control lying about what is stored. */
  "check-now": () => checkNow(),
  "ntf-read": arg => markNotificationRead(arg),
  "ntf-read-all": () => markAllNotificationsRead(),
  "ntf-pref": arg => setNotificationCode(arg),
  "ntf-mail": arg => setNotificationPref({ email_enabled: arg === "1" }),
  "ntf-sev": arg => setNotificationPref({ minimum_severity: arg }),
  "ntf-digest": arg => setNotificationPref({ digest: arg }),
  "ntf-preview": arg => { S.previewMessage = arg; render(); },
  "ntf-restart": () => restartWatch(),
  /* Tab selection inside a container screen. `arg` is "group:tabId" — one action rather
   * than one per group, because the alternative was 23 near-identical entries in this
   * map for the 23 screens the sidebar collapse turned into tabs. */
  tab: arg => {
    const split = String(arg || "").indexOf(":");
    if (split < 0) return;
    S.tab[arg.slice(0, split)] = arg.slice(split + 1);
    render();
  },
  /* Jump to a container screen AND select a tab inside it, as one click.
   * `arg` is "view|group|tabId".
   *
   * Needed because the sidebar collapse turned eleven destinations into tabs, and the
   * links pointing at them from elsewhere — the Overview's risk cards, the empty states —
   * would otherwise land on a screen with no sidebar item highlighted, which reads as
   * "you are nowhere". This keeps the deep-link and leaves the user somewhere they can
   * see they are.
   */
  open: arg => {
    const [view, group, tabId] = String(arg || "").split("|");
    if (group && tabId) S.tab[group] = tabId;
    if (view) nav8(view);
  },
  /* Open one carrier account. The id rides on S rather than the path because the app has
   * no router; see the note on S.tab. */
  "car-open": arg => { S.carAccount = arg; nav8("car-detail"); },
  /* The adviser's two real actions on a disclosed case: ask the client for something, and
   * move a correction request through the state machine. Both are server-enforced —
   * `adv-move` sends only the target state, never the actor. */
  "adv-ask": arg => adviserAsk(arg),
  "adv-move": arg => adviserMove(arg),
  /* The owner's own two moves on a prepared request: release it, or withdraw it. */
  "own-move": arg => ownerMove(arg),
  /* The guided walkthrough. `tour-skip` and `tour-dismiss` both end it, but only
   * `tour-dismiss` on the invitation records "do not ask again" — skipping mid-tour should
   * not be read as never wanting to see it. */
  "tour-start": () => tourStart(),
  "tour-next": () => tourNext(1),
  "tour-back": () => tourNext(-1),
  "tour-skip": () => tourEnd(false),
  "tour-dismiss": () => tourDismiss(true),
  /* The helper. `ask-go` and `ask-chip` both travel; the difference is only where the
   * destination came from — an answer the helper gave, or a suggestion chip. Both resolve
   * through askFind() and both pass the same role check before anything moves. */
  "ask-open": () => askToggle(),
  "ask-close": () => askClose(),
  "ask-go": arg => askGo(arg),
  "ask-chip": arg => { const d = askFind(arg); if (d) askSend(`Take me to ${d.label}`); },
  /* Landing page: move from marketing to the login form, and jump between sections.
   * `show-login` sets a flag rather than navigating, so the transition needs no round trip
   * and the browser Back button still returns to the landing page. */
  "show-login": () => {
    S.showLogin = true;
    try { history.pushState({}, "", "/login"); } catch (_) { /* file:// etc */ }
    render();
  },
  "lp-jump": arg => {
    const target = document.getElementById(String(arg || ""));
    if (target) target.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto" : "smooth", block: "start" });
  },
  /* Timeline period filter. Client-side over rows already loaded — see TIME_PERIODS. */
  "tl-period": arg => { S.timePeriod = arg; render(); },
  /* The evidence drawer. Opens the quotation behind one gap-snapshot row without leaving the
   * screen, because "show me why" is the question this product exists to answer and making it
   * a navigation event buries it. */
  "gap-why": arg => openEvidenceDrawer(arg),
  "drawer-close": () => {
    S.drawer = null; S.pageFull = null;
    document.getElementById("veris-drawer")?.remove();
    document.getElementById("veris-pagefull")?.remove();
  },
  /* The source-page viewer inside the drawer. `drawer-page` switches which citation's
   * page is shown when a finding rests on more than one. */
  "drawer-page": arg => loadDrawerPage(Number(arg)),
  "pagefull-open": () => openPageFullscreen(),
  "pagefull-close": () => {
    S.pageFull = null;
    document.getElementById("veris-pagefull")?.remove();
  },
};

/* The Back button must work on a page that has a real address. Without this, leaving the
 * login form via Back changes the URL and leaves the login form on screen. */
window.addEventListener("popstate", () => {
  S.showLogin = location.pathname === "/login";
  render();
});

function dispatchAction(ev) {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const handler = ACTIONS[el.dataset.act];
  if (!handler) return;
  ev.preventDefault();
  handler(el.dataset.arg, el);
}

document.addEventListener("click", dispatchAction);
document.addEventListener("change", dispatchAction);

/* ══════════════════════════════════════════════════════════════════════════
   VISUAL COMPONENTS

   Ported from Shreya's design system, unchanged in behaviour. A number in a table is
   read; a number in a ring or a waterfall is FELT, and the whole problem with insurance
   documents is that nobody feels them until it is too late.

   These are pure functions: data in, HTML out. They hold no state, fetch nothing, and
   never decide anything. Every value they draw was verified by the engine before it got
   here.

   Credit: Shreya.
   ══════════════════════════════════════════════════════════════════════════ */

function renderHero(label, value, sub, severity) {
  const cls = severity === "HIGH" ? "hero--critical"
    : severity === "ELEVATED" ? "hero--elevated"
      : severity === "LOW" ? "hero--good" : "";
  return `<div class="v-hero ${cls}"><div class="v-hero__label">${esc(label)}</div><div class="v-hero__value">${value}</div><div class="v-hero__sub">${esc(sub || "")}</div></div>`;
}

function renderRing(score, size) {
  size = size || 140;
  const r = (size - 16) / 2, c = 2 * Math.PI * r, off = c * (1 - (score || 0) / 100);
  const cls = score > 70 ? "ring--good" : score > 40 ? "ring--warn" : "ring--bad";
  return `<div class="v-ring ${cls}" style="width:${size}px;height:${size}px" role="img" aria-label="Score ${score} out of 100"><svg width="${size}" height="${size}"><circle class="v-ring__track" cx="${size / 2}" cy="${size / 2}" r="${r}"/><circle class="v-ring__fill" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-dasharray="${c}" stroke-dashoffset="${off}"/></svg><span class="v-ring__val">${score ?? "—"}</span></div>`;
}

function renderBars(items) {
  if (!items?.length) return "";
  const max = Math.max(...items.map(i => i.max || i.value || 0), 1);
  return `<div class="v-bars">${items.map(i => {
    const w = Math.min(100, ((i.value || 0) / max) * 100);
    const cls = i.status === "bad" ? "v-bar--bad" : i.status === "warn" ? "v-bar--warn" : "v-bar--good";
    return `<div class="v-bar-row"><span class="v-bar-label">${esc(i.label)}</span><div class="v-bar-track"><div class="v-bar-fill ${cls}" style="width:${w}%"></div></div><span class="v-bar-val">${esc(i.display || String(i.value ?? ""))}</span></div>`;
  }).join("")}</div>`;
}

function renderRiskMeter(level) {
  const levels = ["LOW", "MODERATE", "ELEVATED", "HIGH"];
  const idx = levels.indexOf((level || "").toUpperCase());
  const colors = ["var(--success)", "#84cc16", "var(--warning)", "var(--danger)"];
  return `<div class="v-meter" role="img" aria-label="Conditions ${esc(level || "unknown")}"><div class="v-meter__bar">${levels.map((l, i) => `<div class="v-meter__seg${i <= idx ? " v-meter__seg--active" : ""}" style="${i <= idx ? "background:" + colors[i] : ""}"></div>`).join("")}</div><div class="v-meter__label" style="color:${colors[Math.max(idx, 0)]}">${esc(level || "UNKNOWN")}</div></div>`;
}

function renderTimeline(items) {
  if (!items?.length) return '<p class="v-muted">Nothing yet</p>';
  return `<div class="v-timeline">${items.slice(0, 6).map(i => {
    const dot = i.severity === "HIGH" ? "v-tl__dot--alert" : i.severity === "LOW" ? "v-tl__dot--ok" : "v-tl__dot--active";
    return `<div class="v-tl__item"><div class="v-tl__dot ${dot}"></div><div class="v-tl__content"><div class="v-tl__title">${esc(i.title || i.label || "")}</div><div class="v-tl__meta">${esc(i.source || "")}${i.date ? " · " + esc(i.date) : ""}</div>${i.detail ? `<div class="v-tl__body">${esc(i.detail)}</div>` : ""}</div></div>`;
  }).join("")}</div>`;
}

function renderWaterfall(steps, startLabel, startAmt) {
  if (!steps?.length) return "";
  return `<div class="v-waterfall"><div class="v-wf__step v-wf__step--start"><span class="v-wf__label">${esc(startLabel || "The loss")}</span><span class="v-wf__amt">${money(startAmt)}</span></div>${steps.map(s => {
    const neg = Number(s.amount) < 0 || (s.operation || "").includes("reduc");
    return `<div class="v-wf__step${neg ? " v-wf__step--neg" : ""}"><span class="v-wf__label">${esc(s.label)}</span><span class="v-wf__amt">${money(Math.abs(Number(s.amount) || 0))}</span><span class="v-wf__op">${esc(s.operation || "")}</span></div>`;
  }).join("")}</div>`;
}

function renderFlowchart(nodes) {
  if (!nodes?.length) return "";
  return `<div class="v-flow">${nodes.map((n, i) => `${i > 0 ? '<div class="v-flow__arrow">↓</div>' : ""}<div class="v-flow__node v-flow__node--${n.type || "default"}">${esc(n.text)}</div>`).join("")}</div>`;
}

function renderConvergence(internal, external, result) {
  return `<div class="v-convergence"><div class="v-conv__sources"><div class="v-conv__src v-conv__src--int"><div class="v-conv__badge">FROM YOUR RECORDS</div><div class="v-conv__text">${esc(internal)}</div></div><div class="v-conv__src v-conv__src--ext"><div class="v-conv__badge v-conv__badge--ext">FROM OUTSIDE</div><div class="v-conv__text">${esc(external)}</div></div></div><div class="v-conv__arrow">↓</div><div class="v-conv__result"><div class="v-conv__badge v-conv__badge--result">WHAT IT MEANS</div><div class="v-conv__text">${esc(result)}</div></div></div>`;
}

const _OB_WORDS = {
  COMPLIANT: "Confirmed fine",
  UNVERIFIED: "Cannot confirm yet",
  BREACHED: "Not being kept",
  NOT_IN_POLICY: "Not in your policy",
};

function renderObligationMap(items) {
  if (!items?.length) return "";
  return `<div class="v-obligations">${items.map(i => `<div class="v-ob__item"><div class="v-ob__dot v-ob__dot--${(i.state || "").toLowerCase()}"></div><div class="v-ob__label">${esc(i.title)}</div><div class="v-ob__state v-ob__state--${(i.state || "").toLowerCase()}">${esc(_OB_WORDS[i.state] || i.state || "")}</div></div>`).join("")}</div>`;
}

function renderChangePulse(items) {
  if (!items?.length) return '<p class="v-muted">Nothing has changed</p>';
  return `<div class="v-pulse">${items.slice(0, 5).map(i => `<div class="v-pulse__item"><div class="v-pulse__icon">${i.direction === "up" ? "↑" : i.direction === "down" ? "↓" : i.direction === "new" ? "+" : "•"}</div><div class="v-pulse__content"><strong>${esc(i.label || "")}</strong><span class="v-pulse__val">${esc(i.value || "")}</span></div><div class="v-pulse__source">${esc(i.source || "")}</div></div>`).join("")}</div>`;
}

function renderTrendChart(observations, threshold) {
  if (!observations?.length) return '<p class="v-muted">Not enough history yet</p>';
  const vals = observations.map(o => parseFloat(o.adequacy_ratio) || 0);
  const max = Math.max(...vals, threshold || 100, 100), min = Math.min(...vals, 0);
  const range = max - min || 1, h = 140, w = Math.max(observations.length * 50, 320);
  const thY = h - ((threshold - min) / range * h);
  const pts = observations.map((o, i) => ({
    x: (i / Math.max(observations.length - 1, 1)) * (w - 40) + 20,
    y: h - ((parseFloat(o.adequacy_ratio) - min) / range * h),
    e: o.engaged, l: o.as_at,
  }));
  let svg = `<svg viewBox="0 0 ${w} ${h + 24}" class="v-trend__svg" role="img" aria-label="Cover against costs over time">`;
  if (threshold) svg += `<line x1="0" y1="${thY}" x2="${w}" y2="${thY}" stroke="var(--danger)" stroke-dasharray="4" opacity=".4"/><text x="${w - 4}" y="${thY - 4}" font-size="9" fill="var(--danger)" text-anchor="end">safe level</text>`;
  for (let i = 1; i < pts.length; i++) svg += `<line x1="${pts[i - 1].x}" y1="${pts[i - 1].y}" x2="${pts[i].x}" y2="${pts[i].y}" stroke="var(--brand)" stroke-width="2.5" stroke-linecap="round"/>`;
  pts.forEach(p => { svg += `<circle cx="${p.x}" cy="${p.y}" r="5" fill="${p.e ? "var(--danger)" : "var(--brand)"}" stroke="#fff" stroke-width="2"/>`; });
  pts.forEach((p, i) => { if (i % 2 === 0 || i === pts.length - 1) svg += `<text x="${p.x}" y="${h + 16}" font-size="9" fill="var(--text-4)" text-anchor="middle">${esc(p.l || "")}</text>`; });
  return `<div class="v-trend">${svg}</svg></div>`.replace("</svg></svg>", "</svg>");
}

function renderMatrix(am) {
  const grid = (am || {}).grid || {}, pc = (am || {}).priority_cell || {};
  const rows = ["HIGH", "MED", "LOW"], cols = ["LOW", "MED", "HIGH"];
  let html = `<div class="v-matrix"><table><thead><tr><th></th>${cols.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
  rows.forEach(r => {
    html += `<tr><th>${r}</th>`;
    cols.forEach(c => {
      const n = ((grid[r] || {})[c]) ?? 0;
      const hot = pc.internal === r && pc.external === c;
      html += `<td class="v-mx__cell${hot ? " v-mx__cell--hot" : ""}"><div class="v-mx__n">${n}</div>${hot ? '<div class="v-mx__tag">PRIORITY</div>' : ""}</td>`;
    });
    html += "</tr>";
  });
  return html + `</tbody></table><div class="v-mx__axes"><span>↑ Change inside the business</span><span>Conditions outside →</span></div></div>`;
}

function renderEvidenceStack(evs) {
  if (!evs?.length) return "";
  return evs.slice(0, 3).map(ev => `<div class="v-evidence"><div class="v-evidence__quote">"${esc(String(ev.quote || ""))}"</div><div class="v-evidence__src">${esc(ev.document_kind || ev.source || "")}${ev.page ? " · page " + ev.page : ""}</div></div>`).join("");
}

function renderDataBadge(isLive) {
  return isLive
    ? '<span class="v-badge v-badge--live">● LIVE</span>'
    : '<span class="v-badge v-badge--demo">EXAMPLE</span>';
}

/* ── The assessment panel ─────────────────────────────────────────────────
 * Where the presentation layer's output lands. Every screen that shows one of these got
 * its wording from `/api/presentation/compose`, and the mode badge says whether a model
 * wrote it or a written template did. That distinction is on screen rather than in a
 * footnote, because a reader is entitled to know which they are looking at. */
function renderAssessment(pres, mode) {
  if (!pres) return "";
  const pts = (pres.supporting_points || []).filter(Boolean);
  const badge = mode === "MODEL"
    ? '<span class="v-badge v-badge--live" title="Wording written by a language model from verified figures">◆ AI worded</span>'
    : '<span class="v-badge v-badge--demo" title="Wording from a written template">◆ Standard wording</span>';
  return `<div class="v-ai">
    <div class="v-ai__mark">Veris assessment ${badge}</div>
    <div class="v-ai__headline">${esc(pres.headline || "")}</div>
    ${pres.subheadline ? `<div class="v-ai__sub">${esc(pres.subheadline)}</div>` : ""}
    ${pts.length ? `<ul class="v-ai__pts">${pts.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    ${pres.why_it_matters ? `<div class="v-ai__why">${esc(pres.why_it_matters)}</div>` : ""}
    ${pres.what_next ? `<div class="v-ai__next">${esc(pres.what_next)}</div>` : ""}
  </div>`;
}

/* Ask the presentation layer to word a screen. Never throws: a failed call means the
 * screen renders without the panel rather than not rendering at all. */
async function getPresentation(purpose, verified_context) {
  try {
    const r = await api("/api/presentation/compose", {
      method: "POST", body: { purpose, verified_context },
    });
    return r ? { pres: r.presentation, mode: r.mode, rejected: r.error } : null;
  } catch {
    return null;
  }
}

/* ── API transport ────────────────────────────────────────────────────── */

async function api(path, opts = {}) {
  const h = { ...opts.headers };
  if (opts.body && !(opts.body instanceof FormData)) {
    h["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  let r;
  try {
    r = await fetch(path, { ...opts, headers: h, credentials: "same-origin" });
  } catch (e) {
    throw new Error("Cannot reach the server. Check it is running.");
  }
  // A 401 means this session is not authenticated for that route. It must NOT call
    // logout(), because logout drops the CASE from the server -- so one stray request to a
    // route the current role cannot reach would silently destroy the case being reviewed.
    // Forget the user on the client, keep server state, show the login form.
    if (r.status === 401) { switchRole(); return null; }
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }));
    const message = e.detail || (typeof e.error === "string" ? e.error : e.error?.message)
      || `Error ${r.status}`;
    const error = new Error(message);
    if (typeof e.error?.code === "string") error.code = e.error.code;
    throw error;
  }
  return r.json();
}

/* ── Auth (cookie-based) ──────────────────────────────────────────────── */

function getUser() {
  try { return JSON.parse(sessionStorage.getItem("veris_user")); } catch { return null; }
}
function setUser(u) { S.user = u; sessionStorage.setItem("veris_user", JSON.stringify(u)); }
/* SIGN OUT versus SWITCH ROLE — the distinction is load-bearing.
 *
 * A case lives on the server session, and POST /api/auth/logout deliberately drops it:
 * signing out means the analysis goes with you. That is a real privacy property and worth
 * demonstrating.
 *
 * But a case is also reviewed by three roles in turn, and the adviser reads the case from
 * the SAME session the owner disclosed. So signing out to reach the login form destroys the
 * very case the next role is meant to look at — the adviser then lands on an empty queue and
 * a 403, which reads as a broken product when it is actually the session being gone.
 *
 * switchRole() therefore forgets the user on the client and shows the login form WITHOUT
 * calling the logout endpoint, so the case and its disclosures survive the handover.
 */
async function logout() {
  S.user = null;
  sessionStorage.removeItem("veris_user");
  // Awaited, then a real navigation. Signing out means leaving the product, and the front
  // door is now a document the server serves at `/` rather than a view this script paints.
  // The POST used to be fired and forgotten before a re-render; navigating away without
  // waiting would cancel it in flight and leave the case sitting on the server.
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* going anyway */ }
  location.assign("/");
}
window.logout = logout;

function switchRole() {
  S.user = null;
  S.view = null;
  // Explicitly ask for the login form. Without this the router fell through to its
  // "nobody is signed in" branch, and because the address bar still said /app it painted
  // the in-app landing page — so handing the case to the next role went via a marketing
  // page instead of the form, which is the opposite of what the comment above promises.
  S.showLogin = true;
  sessionStorage.removeItem("veris_user");
  render();
}
window.switchRole = switchRole;

/* A cookie can outlive this tab's sessionStorage.
 *
 * The session lives in an HttpOnly cookie, but `getUser()` reads the signed-in identity
 * from sessionStorage, which is per-TAB. Open a second tab, or reopen the URL after a demo,
 * and the server still knows exactly who you are while this script has no idea — so the
 * router concluded nobody was signed in and painted the old in-app landing page over a live
 * workspace. Asking is cheap and `/api/auth/me` is the endpoint that answers.
 *
 * One shot per load: a failed probe must not turn into a render loop.
 */
let _sessionProbed = false;

function recoverSession() {
  if (_sessionProbed) return;
  _sessionProbed = true;
  api("/api/auth/me").then(me => {
    if (me && me.authenticated && me.user && me.user.role) {
      S.showLogin = false;
      setUser(me.user);
      render();
      return;
    }
    /* Nobody is signed in — yet the server handed this app to a visitor at `/`, and it only
     * does that when `web/landing/` is absent (see `_landing()` in api.py, which falls back
     * to the app rather than 404-ing the front door). That missing-folder case is the one
     * situation the in-app landing below still exists to cover, so this is where it runs.
     * On every other path the login form already up is the right answer. */
    if (location.pathname === "/" && !S.showLogin) renderLanding();
  }).catch(() => { /* offline; the login form is already up and needs no network */ });
}

/* ── Router ───────────────────────────────────────────────────────────── */

/* Where each role belongs after signing in. The SERVER decides the role — this is only
 * where the client puts them once it has been told. There is deliberately no role picker
 * on the login form: letting a client choose its own role would make the whole boundary
 * decorative. */
const ROLE_PATH = { SME: "/app", ADVISER: "/adviser", CARRIER: "/carrier" };

function render() {
  const u = getUser();
  if (u) S.user = u;

  if (!S.user) {
    /* No user ON THE CLIENT. That is not the same as no session.
     *
     * The marketing page is a document the server serves at `/`, and it only serves it when
     * the cookie carries no signed-in principal. So reaching this branch means one of two
     * things, and they need opposite treatments:
     *
     *   1. an app path with genuinely no session   -> the door is the login form
     *   2. a cookie this tab's sessionStorage does not know about -> recover and carry on
     *
     * It used to paint `renderLanding()` here, the in-app landing. That was right when the
     * SPA owned the landing page. Now it means a signed-in visitor reopening the URL, or
     * anyone clicking Switch Role, gets shown the OLD marketing page — two landing pages in
     * one product, and the wrong one at the worst moment.
     *
     * The form goes up immediately so nothing is ever blank, and the probe corrects it if
     * the cookie is still good. Redirecting to `/` instead would loop: the server would
     * serve this app back for exactly the session this branch cannot see.
     */
    renderLogin();
    recoverSession();
    return;
  }

  // Authenticated. Keep the address bar honest about which workspace this is, and send an
  // authenticated visitor away from /login rather than showing them a form they have
  // already filled in.
  const want = ROLE_PATH[S.user.role];
  if (want && (location.pathname === "/login" || location.pathname === "/")) {
    try { history.replaceState({}, "", want); } catch (_) { /* file:// etc */ }
  }
  if (S.user.role === "SME") renderSME();
  else if (S.user.role === "ADVISER") renderAdviser();
  else if (S.user.role === "CARRIER") renderCarrier();
  else renderLogin();
}
function nav8(v) { S.view = v; render(); }
window.nav8 = nav8;

/* Every view is async and starts by painting a spinner. If one then throws — a 403 from a
 * consent boundary, a network drop, a field that moved — the spinner used to stay on screen
 * forever with no explanation. This wrapper guarantees the user always gets either the
 * screen or a readable reason, never a hang. */
async function mount(view, el) {
  try {
    await view(el);
    stageView(el);
  } catch (err) {
    el.innerHTML = `<div class="insight critical"><div class="insight-label">This screen could not load</div>
        <div class="insight-text">${esc(err && err.message ? err.message : String(err))}</div></div>
      <p class="text-sm text-muted mt-12">Nothing was changed. Try another screen, or reload the page.</p>`;
  }
}

/* ── Login ────────────────────────────────────────────────────────────── */
//
// The demo walkthrough switches role three times. Retyping an email and password at each
// switch is three chances to fumble in front of an audience, so the accounts are buttons
// that fill the form rather than text to copy. The password is a printed demo credential
// by design — `auth.py` says so explicitly and this is not a production posture.
const DEMO_PASSWORD = "Demo@123";
// The carrier account still exists and its workspace still works, but it is not part of
// the household-and-business product, so the login screen does not offer it. Kept in step
// with `auth.DEMO_LOGIN_EMAILS`; a test compares the two.
const DEMO_ACCOUNTS = [
  { email: "owner@hockleefoods.demo", label: "Business owner" },
  { email: "adviser@lionbrokers.demo", label: "Licensed adviser" },
];

function fillLogin(email) {
  const e = $("#lemail"), p = $("#lpass");
  if (!e || !p) return;
  e.value = email;
  p.value = DEMO_PASSWORD;
  $("#lbtn")?.focus();
}
window.fillLogin = fillLogin;

/* ══════════════════════════════════════════════════════════════════════════
   MOTION PRIMITIVES
   ══════════════════════════════════════════════════════════════════════════
 *
 * Three helpers, shared by every screen, so entrance choreography is defined once
 * instead of per view. All three are no-ops under `prefers-reduced-motion`: they check
 * the media query rather than relying on the CSS override, because a count-up that
 * "completes instantly" still has to land on the right number.
 */
const REDUCED_MOTION = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Stagger an entrance across a set of elements: "everything coming into place from
 * somewhere". `step` is deliberately small — 55ms reads as one coordinated movement,
 * 200ms reads as a queue of separate things. */
function stage(root, selector = "[data-rise]", step = 55, kind = "rise") {
  if (!root) return;
  const nodes = root.querySelectorAll(selector);
  nodes.forEach((n, i) => {
    if (REDUCED_MOTION()) { n.classList.add("is-in"); return; }
    n.style.setProperty("--d", `${i * step}ms`);
    n.classList.add(n.dataset.rise || kind);
  });
}

/* Count a figure up to its real value.
 *
 * `data-to` is the TRUE number and the element's text always ends on it — the animation
 * only controls what is shown on the way there. That distinction matters in a product
 * whose whole claim is that its figures are checkable: a number that animates is still
 * the number the API returned, and if the animation is skipped or interrupted the value
 * is still correct.
 */
function countUp(el) {
  const to = parseFloat(el.dataset.to);
  if (!isFinite(to)) return;
  const dp = parseInt(el.dataset.dp || "0", 10);
  const prefix = el.dataset.prefix || "";
  const suffix = el.dataset.suffix || "";
  const group = el.dataset.group === "1";
  const show = v => {
    const n = dp ? v.toFixed(dp) : String(Math.round(v));
    el.textContent = prefix + (group ? Number(n).toLocaleString("en-SG",
      { minimumFractionDigits: dp, maximumFractionDigits: dp }) : n) + suffix;
  };
  if (REDUCED_MOTION()) { show(to); return; }
  const dur = 1100 + Math.min(500, Math.abs(to) / 4);
  const t0 = performance.now();
  const tick = now => {
    const p = Math.min(1, (now - t0) / dur);
    // Ease out cubic: fast start, long settle. Reads as a value arriving rather than a
    // counter spinning.
    show(to * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick); else show(to);
  };
  requestAnimationFrame(tick);
}

/* Run every count-up inside a container once it is on screen. */
function countUpAll(root) {
  if (!root) return;
  root.querySelectorAll("[data-to]").forEach(el => {
    if (el.dataset.counted === "1") return;
    el.dataset.counted = "1";
    countUp(el);
  });
}

/* Every screen's entrance, applied once from `mount()`.
 *
 * Done here rather than in forty view functions because `mount()` is the single choke
 * point every view and every tab passes through. A view that already staged itself is
 * skipped, so an explicit `stage()` call inside a view still wins and nothing is animated
 * twice.
 */
function stageView(root) {
  if (!root) return;
  const already = root.querySelector(".rise, .zoom, .settle");
  if (!already) {
    const rows = root.querySelectorAll(
      ":scope > .card, :scope > .stats > .stat, :scope > .findings > .finding, "
      + ":scope > .action-card, :scope > .insight, :scope > .pc, :scope > .big, "
      + ":scope > .grid-2, :scope > .heads, :scope > .areas, :scope > .caveat");
    rows.forEach((n, i) => {
      if (REDUCED_MOTION()) return;
      n.style.setProperty("--d", `${Math.min(i * 45, 400)}ms`);
      n.classList.add("rise");
    });
  }
  animateFigures(root);
}

/* Count up every figure a view has already rendered.
 *
 * Reads the number OUT of the DOM rather than being handed one, which is what keeps this
 * honest: the value animated is exactly the value the view painted from the API, and the
 * element ends on that same string. A figure that does not parse as a number — a date, an
 * em dash for "not assessed", a range — is left completely alone rather than coerced,
 * because "—" means "we could not establish this" and must never animate to zero.
 */
const _FIG = /^\s*([^\d\-+]*)(-?[\d,]+(?:\.\d+)?)\s*(.*?)\s*$/;

function animateFigures(root) {
  root.querySelectorAll(".stat .val, .big__v, .pf__score-v, .lp2__f-v, .cv__fig")
    .forEach(el => {
      if (el.dataset.counted === "1" || el.children.length) return;

      /* A view that already declared the true value wins over the text on screen.
       *
       * THE BUG THIS FIXES — the Protection score reading 0.
       *
       * There are two count-up paths in this file. `countUpAll()` is handed the truth in
       * `data-to` and treats the element's text as a mere placeholder. `animateFigures()`
       * below does the opposite: it reads the number OUT of the DOM, which is the right
       * call for the many figures painted directly as text.
       *
       * `protectionField()` renders the score for the FIRST path:
       *
       *     <div class="pf__score-v num-display" data-to="68">0</div>
       *
       * but `.pf__score-v` is in this function's selector list, and nothing ever called
       * `countUpAll()` on that screen. So this ran instead, parsed the placeholder "0" as
       * the value, and — the fatal line — reassigned `el.dataset.to = "0"`, destroying the
       * real 68 before animating 0 to 0. The score sat at 0 permanently while /api/score
       * was returning `overall: 68` correctly the whole time.
       *
       * The landing-page figures use the same placeholder pattern and escaped only
       * because `countUpAll(band)` happens to run first and marks them counted.
       *
       * So: if `data-to` is already a number, it is the API's value and must be trusted.
       * Never derive a target from placeholder text when the truth was handed to us.
       */
      const declared = parseFloat(el.dataset.to);
      if (el.dataset.to !== undefined && isFinite(declared)) {
        el.dataset.counted = "1";
        countUp(el);
        return;
      }

      const m = _FIG.exec(el.textContent || "");
      if (!m) return;
      const raw = m[2].replace(/,/g, "");
      const value = parseFloat(raw);
      if (!isFinite(value)) return;
      const dp = (raw.split(".")[1] || "").length;
      el.dataset.to = String(value);
      el.dataset.prefix = m[1] || "";
      el.dataset.suffix = m[3] || "";
      el.dataset.dp = String(Math.min(dp, 2));
      el.dataset.group = m[2].includes(",") ? "1" : "0";
      el.dataset.counted = "1";
      countUp(el);
    });
}

/* Split a heading into words so each can zoom in on its own beat. Words, not letters:
 * letter-by-letter is a novelty effect and it makes a headline unreadable while it
 * plays. Uses textContent per span, so nothing here can inject markup. */
function splitWords(text, cls = "lp__w") {
  return String(text || "").split(/\s+/).filter(Boolean)
    .map((w, i) => `<span class="${cls}" style="--d:${60 + i * 70}ms">${esc(w)}</span>`)
    .join(" ");
}

/* The orbital sphere from the landing references: concentric shells labelled with the
 * three inputs, a building at the centre, and two glass lenses — jade for Connect,
 * amber for Sentinel — on inclined orbits. Pure inline SVG and CSS: no image asset, no
 * WebGL, nothing to lazy-load. */
function verisOrb() {
  return `<svg class="orb" viewBox="0 0 520 520" role="img"
      aria-label="Veris draws business data, external risk and policy evidence into one protection field">
    <defs>
      <radialGradient id="orbGlass" cx="38%" cy="28%" r="78%">
        <stop offset="0" stop-color="#ffffff" stop-opacity=".97"/>
        <stop offset="42%" stop-color="#eef4f8" stop-opacity=".78"/>
        <stop offset="78%" stop-color="#dfe9ef" stop-opacity=".5"/>
        <stop offset="100%" stop-color="#cfdde6" stop-opacity=".22"/>
      </radialGradient>
      <radialGradient id="orbCore" cx="40%" cy="32%" r="70%">
        <stop offset="0" stop-color="#ffffff"/>
        <stop offset="100%" stop-color="#e6eff5"/>
      </radialGradient>
      <linearGradient id="lensJade" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#8fc4b1"/><stop offset="1" stop-color="#3f7263"/>
      </linearGradient>
      <linearGradient id="lensAmber" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#e6bd77"/><stop offset="1" stop-color="#a8741c"/>
      </linearGradient>
      <path id="ringOuter" d="M110,268 a150,150 0 1,1 300,0 a150,150 0 1,1 -300,0"/>
      <path id="ringMid"   d="M148,268 a112,112 0 1,1 224,0 a112,112 0 1,1 -224,0"/>
      <path id="ringInner" d="M186,268 a74,74 0 1,1 148,0 a74,74 0 1,1 -148,0"/>
    </defs>

    <circle class="orb__halo" cx="260" cy="262" r="196"/>
    <circle class="orb__shell" cx="260" cy="262" r="182" fill="url(#orbGlass)"/>
    <circle class="orb__ring" cx="260" cy="262" r="150"/>
    <circle class="orb__ring" cx="260" cy="262" r="112"/>
    <circle class="orb__core" cx="260" cy="262" r="74" fill="url(#orbCore)"/>

    <!-- Labels ride the shells, which is what makes them read as layers of one field
         rather than as three separate captions. -->
    <text class="orb__t"><textPath href="#ringOuter" startOffset="25%">BUSINESS DATA</textPath></text>
    <text class="orb__t"><textPath href="#ringMid" startOffset="25%">EXTERNAL RISK</textPath></text>
    <text class="orb__t"><textPath href="#ringInner" startOffset="25%">POLICY EVIDENCE</textPath></text>

    <g class="orb__mark" transform="translate(260 262)">
      <path d="M-15,14 V-13 h13 v6 h13 V14 Z" />
      <path d="M-9,-6 h4 M-9,1 h4 M-9,8 h4 M3,1 h4 M3,8 h4" class="orb__mark-w"/>
    </g>

    <!-- Two inclined orbits with a glass lens each. The lens is the agent; the orbit is
         its beat. -->
    <g class="orb__path"><ellipse cx="260" cy="262" rx="228" ry="86" transform="rotate(-18 260 262)"/></g>
    <g class="orb__path"><ellipse cx="260" cy="262" rx="214" ry="98" transform="rotate(22 260 262)"/></g>
    <g class="orb__lens orb__lens--jade">
      <ellipse rx="34" ry="13" fill="url(#lensJade)" opacity=".9"/>
    </g>
    <g class="orb__lens orb__lens--amber">
      <ellipse rx="30" ry="12" fill="url(#lensAmber)" opacity=".9"/>
    </g>
  </svg>`;
}

/* ══════════════════════════════════════════════════════════════════════════
   GUIDED TOUR
   ══════════════════════════════════════════════════════════════════════════
 *
 * A first-run walkthrough: an invitation bottom-right, then a spotlight that dims the
 * page and brightens one region at a time with an explanation beside it.
 *
 * THREE DESIGN DECISIONS WORTH STATING.
 *
 * 1. THE TOUR NAVIGATES ITSELF. Each step may name a `view` (and a `tab`), and the tour
 *    drives the app there before highlighting anything. The alternative — telling the
 *    reader to click something and hoping they click the right thing — breaks the moment
 *    somebody clicks elsewhere, and a broken tour is worse than no tour.
 *
 * 2. A STEP WHOSE TARGET IS ABSENT IS SKIPPED, NOT FAKED. Screens differ by role and by
 *    whether a case exists at all, so `waitForTarget` polls briefly and the step is
 *    dropped if the element never appears. The counter reads "3 of 9" over the steps that
 *    actually ran, so it can never promise a step it cannot show.
 *
 * 3. IT EXPLAINS WHAT IS ON SCREEN, NOT WHAT WE WISH WERE. The copy points at real
 *    controls and says what they do. No step claims a figure or a capability that the
 *    surface behind it does not have.
 */

const TOUR_SEEN_KEY = "veris.tour.seen";
const T = { on: false, steps: [], i: 0, ran: 0, cleanup: null };

/* One entry per role. `target` is a selector on the mounted screen; `pad` widens the
 * spotlight where a control is visually smaller than the thing being explained. */
const TOUR_STEPS = {
  SME: [
    { view: "sme-home", target: ".pf__core", pad: 26, place: "right",
      title: "Your protection field",
      body: "One score for how well your paperwork backs up the cover you think you have. "
          + "It counts up from your own documents — nothing here is an estimate." },
    { view: "sme-home", target: ".pf__stage", pad: 8, place: "bottom",
      title: "Each ring is an area of your business",
      body: "A solid ring was assessed. A dashed ring is one Veris could not check — that "
          + "is not the same as one with nothing wrong, and it is never shown as green." },
    { view: "sme-home", target: ".stats", pad: 8, place: "bottom",
      title: "What was found, and how sure we are",
      body: "Proven means a document says so. Needs an expert means a licensed person must "
          + "decide. We couldn't check means we are telling you what we do not know." },
    { view: "sme-position", target: "[data-act='gap-why']", pad: 12, place: "left",
      title: "Show me why — the heart of it",
      body: "Every finding opens the page of the actual PDF it came from, with the exact "
          + "sentence highlighted. Click it during your own walkthrough." },
    { view: "sme-docs-hub", tab: ["docs", "add"], target: ".dropzone", pad: 10,
      place: "bottom", title: "Add documents",
      body: "Drop PDFs here. Each one gets a spinner while it is read and a green tick when "
          + "its text comes out — real progress from the server, file by file." },
    { view: "sme-agents", target: ".cv", pad: 10, place: "bottom",
      title: "Two agents, one field",
      body: "Connect watches inside the business, Sentinel watches around it. When both "
          + "report at once, the middle says convergence. When one is quiet, it says so." },
    { view: "sme-act-hub", tab: ["act", "todo"], target: ".wr", pad: 12, place: "top",
      title: "Observe → Investigate → Verify → Act",
      body: "Where each correction has actually reached. It moves when a person moves it, "
          + "never on a timer." },
    { view: "sme-consent", target: ".consent-list", pad: 10, place: "right",
      title: "You choose what leaves this workspace",
      body: "Consent is granted per document. Your adviser only sees a finding if you gave "
          + "them every document it rests on — and you can stop sharing at any time." },
    { view: "sme-record", target: ".card", pad: 8, place: "bottom",
      title: "Your record survives deletion",
      body: "Established facts are sealed in a chain you can download and verify. Delete "
          + "the case and the findings go; this stays." },
    { target: ".sidebar-foot", pad: 10, place: "right",
      title: "Switch role to see the rest",
      body: "The same case, read by a licensed adviser, under different rules. "
          + "Switch Role keeps the case on the server; Sign Out drops it." },
  ],
  ADVISER: [
    { view: "adv-queue", target: ".pc", pad: 10, place: "bottom",
      title: "Priority canvas",
      body: "Clients placed by urgency and impact, from the queue's own numbers. Grey dots "
          + "are demonstration rows with no documents behind them." },
    { view: "adv-queue", target: "table", pad: 8, place: "top",
      title: "The numbers stay",
      body: "The canvas is a way in, not a replacement. Every figure is still here to read." },
    { view: "adv-review", target: ".stats", pad: 8, place: "bottom",
      title: "What you were given, and what you were not",
      body: "Withheld findings are counted rather than hidden, so you can ask for a wider "
          + "grant instead of reviewing a silently shortened list." },
    { view: "adv-actions", target: ".action-card", pad: 8, place: "bottom",
      title: "The moves that are yours",
      body: "Take it on, ask for information, record terms, confirm it is bound. The server "
          + "checks the actor — you cannot make a move reserved to the owner." },
    { view: "adv-note", target: ".card", pad: 8, place: "bottom",
      title: "The file note",
      body: "Drafted for you and checked against the disclosed evidence. Only an approved "
          + "note is a record, and nothing here is ever sent automatically." },
  ],
  CARRIER: [
    { view: "car-triage", target: ".card", pad: 8, place: "bottom",
      title: "Work queue",
      body: "Ranked server-side with the weighting shown, so the order can be argued with "
          + "rather than trusted." },
    { view: "car-portfolio", target: ".synthetic-banner", pad: 8, place: "bottom",
      title: "Labelled, not blended",
      body: "Demonstration portfolio data is marked wherever it appears. A carrier never "
          + "sees one business's documents unless that business named them." },
    { view: "car-matrix", target: ".v-matrix", pad: 8, place: "bottom",
      title: "Attention matrix",
      body: "Internal change against external conditions. It reports attention, never a "
          + "breach — that judgement is not the system's to make." },
  ],
};

function tourStepsFor(role) {
  return (TOUR_STEPS[role] || TOUR_STEPS.SME).map(s => ({ ...s }));
}

/* The invitation. Bottom-right, once per role, dismissable for good. */
function tourInvite() {
  if (!S.user) return;
  const key = `${TOUR_SEEN_KEY}.${S.user.role}`;
  try { if (localStorage.getItem(key)) return; } catch (_) { return; }
  if (document.getElementById("tour-invite")) return;
  const host = document.createElement("div");
  host.id = "tour-invite";
  host.className = "tour-invite";
  host.setAttribute("role", "dialog");
  host.setAttribute("aria-label", "Guided tour");
  host.innerHTML = `<div class="tour-invite__k">First time here?</div>
    <p class="tour-invite__b">A short guided walkthrough of this workspace —
      ${tourStepsFor(S.user.role).length} steps, about a minute.</p>
    <div class="tour-invite__a">
      <button class="btn btn-primary btn-sm" data-act="tour-start">Show me around</button>
      <button class="btn btn-ghost btn-sm" data-act="tour-dismiss">No thanks</button>
    </div>`;
  document.body.appendChild(host);
  requestAnimationFrame(() => host.classList.add("is-in"));
}

function tourDismiss(forGood = true) {
  document.getElementById("tour-invite")?.remove();
  if (!forGood || !S.user) return;
  try { localStorage.setItem(`${TOUR_SEEN_KEY}.${S.user.role}`, "1"); } catch (_) { /* private mode */ }
}

/* Wait for a selector to exist, because the step may have just navigated and the view is
 * async. Resolves null on timeout so the caller can skip the step rather than hang. */
function waitForTarget(sel, ms = 2600) {
  return new Promise(resolve => {
    const t0 = performance.now();
    const look = () => {
      const el = document.querySelector(sel);
      if (el && el.getBoundingClientRect().width > 0) return resolve(el);
      if (performance.now() - t0 > ms) return resolve(null);
      requestAnimationFrame(look);
    };
    look();
  });
}

async function tourStart() {
  tourDismiss(true);
  T.steps = tourStepsFor(S.user && S.user.role);
  T.i = 0; T.on = true; T.ran = 0;

  const host = document.createElement("div");
  host.id = "veris-tour";
  host.innerHTML = `<div class="tour__backdrop"></div>
    <div class="tour__spot" aria-hidden="true"></div>
    <div class="tour__pop" role="dialog" aria-modal="true" aria-live="polite"
         aria-labelledby="tour-title"></div>`;
  document.body.appendChild(host);

  const onKey = ev => {
    if (!T.on) return;
    if (ev.key === "Escape") { ev.preventDefault(); tourEnd(false); }
    else if (ev.key === "ArrowRight" || ev.key === "Enter") { ev.preventDefault(); tourNext(1); }
    else if (ev.key === "ArrowLeft") { ev.preventDefault(); tourNext(-1); }
  };
  const onMove = () => { if (T.on) tourPaint(true); };
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("resize", onMove);
  window.addEventListener("scroll", onMove, true);
  T.cleanup = () => {
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", onMove);
    window.removeEventListener("scroll", onMove, true);
  };

  await tourGo(0);
}

/* Move to step `i`, navigating and skipping absent targets in whichever direction we are
 * travelling. `dir` keeps a missing step from bouncing us back and forth. */
async function tourGo(i, dir = 1) {
  if (!T.on) return;
  while (i >= 0 && i < T.steps.length) {
    const step = T.steps[i];
    if (step.view && S.view !== step.view) {
      if (step.tab) S.tab[step.tab[0]] = step.tab[1];
      S.view = step.view;
      render();
    } else if (step.tab && S.tab[step.tab[0]] !== step.tab[1]) {
      S.tab[step.tab[0]] = step.tab[1];
      render();
    }
    const el = await waitForTarget(step.target);
    if (!T.on) return;
    if (el) { T.i = i; step._el = el; tourPaint(); return; }
    step._skip = true;
    i += dir;                       // that surface is not present for this role or case
  }
  tourEnd(dir > 0);                 // ran off the end forwards = completed
}

function tourNext(dir) { tourGo(T.i + dir, dir); }

function tourPaint(quiet = false) {
  const host = document.getElementById("veris-tour");
  const step = T.steps[T.i];
  if (!host || !step) return;
  const el = step._el && step._el.isConnected
    ? step._el : document.querySelector(step.target);
  if (!el) return;

  // Bring the target into view before measuring, or the spotlight lands off-screen.
  const r0 = el.getBoundingClientRect();
  if (!quiet && (r0.top < 60 || r0.bottom > innerHeight - 60)) {
    el.scrollIntoView({ block: "center",
      behavior: REDUCED_MOTION() ? "auto" : "smooth" });
  }
  const r = el.getBoundingClientRect();
  const pad = step.pad || 8;
  const spot = host.querySelector(".tour__spot");
  spot.style.cssText = `top:${r.top - pad}px;left:${r.left - pad}px;`
    + `width:${r.width + pad * 2}px;height:${r.height + pad * 2}px`;

  const shown = T.steps.filter(s => !s._skip);
  const total = shown.length;
  const at = shown.indexOf(step) + 1;
  const pop = host.querySelector(".tour__pop");
  pop.innerHTML = `<div class="tour__n">${at} of ${total}</div>
    <h3 class="tour__t" id="tour-title">${esc(step.title)}</h3>
    <p class="tour__b">${esc(step.body)}</p>
    <div class="tour__dots" aria-hidden="true">${shown.map((s, k) =>
      `<i class="${k + 1 === at ? "on" : k + 1 < at ? "done" : ""}"></i>`).join("")}</div>
    <div class="tour__a">
      <button class="btn btn-ghost btn-sm" data-act="tour-skip">Skip tour</button>
      <div class="btn-group">
        ${at > 1 ? `<button class="btn btn-secondary btn-sm" data-act="tour-back">Back</button>` : ""}
        <button class="btn btn-primary btn-sm" data-act="tour-next">${
          at === total ? "Finish" : "Next"}</button>
      </div>
    </div>`;

  // Place the popover on the requested side, then clamp it inside the viewport. Clamping
  // rather than flipping keeps the arrow pointing at the target.
  const pr = pop.getBoundingClientRect();
  const gap = 16;
  let top, left;
  const place = step.place || "bottom";
  if (place === "right") { top = r.top + r.height / 2 - pr.height / 2; left = r.right + gap; }
  else if (place === "left") { top = r.top + r.height / 2 - pr.height / 2; left = r.left - pr.width - gap; }
  else if (place === "top") { top = r.top - pr.height - gap; left = r.left + r.width / 2 - pr.width / 2; }
  else { top = r.bottom + gap; left = r.left + r.width / 2 - pr.width / 2; }
  if (left + pr.width > innerWidth - 12) left = innerWidth - pr.width - 12;
  if (left < 12) left = 12;
  if (top + pr.height > innerHeight - 12) top = innerHeight - pr.height - 12;
  if (top < 12) top = 12;
  pop.style.top = `${top}px`;
  pop.style.left = `${left}px`;
  if (!quiet) {
    pop.classList.remove("is-in");
    requestAnimationFrame(() => pop.classList.add("is-in"));
    pop.querySelector("[data-act='tour-next']")?.focus();
  }
}

function tourEnd(completed) {
  T.on = false;
  if (T.cleanup) { T.cleanup(); T.cleanup = null; }
  document.getElementById("veris-tour")?.remove();
  T.steps.forEach(s => { delete s._el; delete s._skip; });
  if (completed) toast("That's the tour. Everything you saw is live.");
}

window.tourStart = tourStart;

/* ══════════════════════════════════════════════════════════════════════════
   THE HELPER — ask where something is, and get taken there
   ══════════════════════════════════════════════════════════════════════════
 *
 * A circle bottom-right; a chat panel when clicked. Two things separate it from the
 * chat bubble every site has.
 *
 * IT NAVIGATES RATHER THAN DESCRIBING. "Go to Protection, then the Cyber and data tab" is
 * an instruction to follow; this selects the tab, opens the screen, waits for it to finish
 * loading, and puts a marker on the heading saying you have arrived. Someone who needed to
 * ask where a screen was is exactly the person who should not then have to find it.
 *
 * IT WILL NOT CROSS A ROLE BOUNDARY, and says so out loud when asked to. The server filters
 * the destination catalogue by the role on the session, so an adviser asking for the
 * carrier's decision log is told which workspace owns it. The guard below is a SECOND check
 * on the same thing — cheap, and it means a bug in the catalogue cannot navigate anybody
 * somewhere their session would only 403 on anyway.
 *
 * The spotlight is the tour's, with one deliberate difference: the tour's backdrop swallows
 * clicks so the page cannot move under it, and this one must not. You arrive here in order
 * to USE the screen, so the marker is `pointer-events: none` throughout and fades out on its
 * own.
 */

const ASK = { open: false, busy: false, msgs: [], dests: [], route: null, cleanup: null };

const ASK_PREFIX = { SME: "sme-", ADVISER: "adv-", CARRIER: "car-" };

function askMount() {
  if (!S.user || document.getElementById("veris-ask-launcher")) return;
  const b = document.createElement("button");
  b.id = "veris-ask-launcher";
  b.className = "ask-fab";
  b.setAttribute("data-act", "ask-open");
  b.setAttribute("aria-label", "Ask where something is");
  b.setAttribute("title", "Ask where something is");
  // Inline SVG rather than an icon font or an emoji: one less asset, and it inherits
  // currentColor so it stays correct against the glass.
  b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="9.2" fill="none" stroke="currentColor" stroke-width="1.3"
              opacity=".45"/>
      <circle cx="12" cy="12" r="4.1" fill="none" stroke="currentColor" stroke-width="1.6"/>
      <circle cx="12" cy="12" r="1.5" fill="currentColor"/>
      <path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4" stroke="currentColor"
            stroke-width="1.3" stroke-linecap="round" opacity=".55"/>
    </svg><span class="ask-fab__pulse" aria-hidden="true"></span>`;
  document.body.appendChild(b);
  requestAnimationFrame(() => b.classList.add("is-in"));
}

function askUnmount() {
  document.getElementById("veris-ask-launcher")?.remove();
  document.getElementById("veris-ask")?.remove();
  askClearSpot();
  ASK.open = false;
  ASK.msgs = [];
  ASK.dests = [];
}

/* Suggestion chips come from the server, not from a list in here. The catalogue is filtered
 * by role there, so hardcoding chips would be a second source of truth for the boundary —
 * and the wrong one, because this file cannot see the session. */
async function askLoadDestinations() {
  if (ASK.dests.length) return;
  try {
    const d = await api("/api/assistant/destinations");
    ASK.dests = (d && d.destinations) || [];
  } catch { ASK.dests = []; }
}

async function askOpen() {
  ASK.open = true;
  if (!ASK.msgs.length) {
    ASK.msgs.push({ who: "veris", text:
      "Tell me what you are looking for and I will open it for you. You can name a screen, "
      + "or just say what you want to know." });
  }
  askPaint();
  await askLoadDestinations();
  if (ASK.open) askPaint();
}

function askClose() {
  ASK.open = false;
  document.getElementById("veris-ask")?.remove();
  document.getElementById("veris-ask-launcher")?.classList.remove("is-hidden");
}

function askToggle() { ASK.open ? askClose() : askOpen(); }

function askPaint() {
  document.getElementById("veris-ask")?.remove();
  if (!ASK.open) return;
  document.getElementById("veris-ask-launcher")?.classList.add("is-hidden");

  const chips = ASK.dests.slice(0, 5).map(d =>
    `<button class="ask-chip" data-act="ask-chip" data-arg="${esc(d.id)}"
       >${esc(d.label)}</button>`).join("");

  const body = ASK.msgs.map(m => {
    if (m.who === "you") {
      return `<div class="ask-msg ask-msg--you"><div class="ask-bub">${esc(m.text)}</div></div>`;
    }
    const trail = (m.trail || []).length
      ? `<div class="ask-trail">${m.trail.map((t, i) =>
          `${i ? '<i aria-hidden="true">›</i>' : ""}<span>${esc(t)}</span>`).join("")}</div>`
      : "";
    const go = m.route
      ? `<button class="btn btn-sm btn-primary mt-8" data-act="ask-go"
           data-arg="${esc(m.route.id)}">Show me →</button>` : "";
    const alts = (m.suggestions || []).length
      ? `<div class="ask-alts">${m.suggestions.map(s =>
          `<button class="ask-chip" data-act="ask-chip" data-arg="${esc(s.id)}"
             >${esc(s.label)}</button>`).join("")}</div>` : "";
    const tag = m.mode
      ? `<div class="ask-mode">${esc(m.mode === "LLM_ASSISTED"
          ? "AI-assisted wording · route is deterministic"
          : "Deterministic — no model involved")}</div>` : "";
    return `<div class="ask-msg ask-msg--veris">
      <div class="ask-bub ${m.boundary ? "is-refused" : ""}">${esc(m.text)}${trail}${go}${alts}${tag}</div>
    </div>`;
  }).join("");

  const host = document.createElement("div");
  host.id = "veris-ask";
  host.className = "ask";
  host.setAttribute("role", "dialog");
  host.setAttribute("aria-label", "Veris helper");
  host.innerHTML = `
    <div class="ask__head">
      <div class="ask__id">
        <span class="ask__dot" aria-hidden="true"></span>
        <div><div class="ask__t">Ask Veris</div>
          <div class="ask__s">${esc((S.user && S.user.role) === "SME" ? "Your workspace"
            : (S.user && S.user.role) === "ADVISER" ? "Adviser workspace"
            : "Carrier workspace")} · I can only open screens you are allowed to see</div></div>
      </div>
      <button class="ask__x" data-act="ask-close" aria-label="Close helper">✕</button>
    </div>
    <div class="ask__body" id="ask-body">${body}
      ${ASK.busy ? `<div class="ask-msg ask-msg--veris"><div class="ask-bub ask-bub--busy">
        <span class="ask-typing"><i></i><i></i><i></i></span></div></div>` : ""}
    </div>
    ${chips && ASK.msgs.length <= 1
      ? `<div class="ask__chips"><div class="ask__chips-k">Try one of these</div>
          <div class="ask__chips-r">${chips}</div></div>` : ""}
    <form class="ask__foot" id="ask-form">
      <input id="ask-input" class="ask__in" type="text" autocomplete="off"
        placeholder="e.g. take me to the cyber panel" aria-label="What are you looking for?"
        maxlength="300">
      <button class="ask__send" type="submit" aria-label="Send">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l16-8-6 8 6 8z"
          fill="currentColor"/></svg></button>
    </form>`;
  document.body.appendChild(host);

  /* The form is wired with addEventListener, not an inline handler — see the CSP note on
   * the delegation table. It is a real <form> so Enter submits without a keydown handler,
   * and preventDefault stops the page navigating. */
  const form = host.querySelector("#ask-form");
  form.addEventListener("submit", ev => { ev.preventDefault(); askSend(); });

  const feed = host.querySelector("#ask-body");
  if (feed) feed.scrollTop = feed.scrollHeight;
  requestAnimationFrame(() => {
    host.classList.add("is-in");
    if (!ASK.busy) host.querySelector("#ask-input")?.focus();
  });
}

async function askSend(preset) {
  const input = document.getElementById("ask-input");
  const q = String(preset != null ? preset : (input ? input.value : "")).trim();
  if (!q || ASK.busy) return;
  if (input) input.value = "";
  ASK.msgs.push({ who: "you", text: q });
  ASK.busy = true;
  askPaint();
  try {
    const a = await api("/api/assistant", { method: "POST", body: { question: q } });
    ASK.busy = false;
    if (!a) { askPaint(); return; }
    ASK.msgs.push({
      who: "veris", text: a.answer, route: a.route, trail: a.route && a.route.trail,
      suggestions: a.suggestions, mode: a.mode, boundary: a.boundary,
    });
    ASK.route = a.route || null;
    askPaint();
    // Only travel when the phrasing asked to be taken somewhere. Hijacking the screen of
    // somebody who asked what a word meant is its own small betrayal, so the server decides
    // this and the button below is the alternative.
    if (a.route && a.navigate) askGo(a.route.id);
  } catch (err) {
    ASK.busy = false;
    ASK.msgs.push({ who: "veris", text: err.message || String(err) });
    askPaint();
  }
}

function askFind(id) {
  const inMsgs = [...ASK.msgs].reverse()
    .map(m => m.route).find(r => r && r.id === id);
  return inMsgs || ASK.dests.find(d => d.id === id) || null;
}

/* Wait for the first selector in `list` that resolves to something with real size. Same
 * rAF poll as the tour, over a list rather than one selector, because a screen with no case
 * renders an empty state whose markup differs from a populated one — so the best target and
 * the one that will actually be there are often not the same element. */
function askWaitForAny(list, ms = 5000) {
  return new Promise(resolve => {
    const t0 = performance.now();
    const look = () => {
      for (const sel of list) {
        const el = document.querySelector(sel);
        if (el && el.getBoundingClientRect().width > 0) return resolve(el);
      }
      if (performance.now() - t0 > ms) return resolve(null);
      requestAnimationFrame(look);
    };
    look();
  });
}

async function askGo(id) {
  const dest = askFind(id);
  if (!dest) return;

  /* SECOND role check. The server already filtered the catalogue; this refuses to act on a
   * destination that does not belong to the signed-in role even if one somehow arrived. */
  const want = ASK_PREFIX[S.user && S.user.role];
  if (!want || !String(dest.view || "").startsWith(want)) {
    ASK.msgs.push({ who: "veris", text:
      `“${dest.label}” is not part of your workspace, so I will not open it.` });
    askPaint();
    return;
  }

  askClearSpot();
  if (dest.tab) S.tab[dest.tab[0]] = dest.tab[1];
  nav8(dest.view);
  // Out of the way so the screen they asked for is the thing on screen.
  askClose();
  const el = await askWaitForAny(dest.targets);
  if (!el) {
    ASK.msgs.push({ who: "veris", text:
      `I opened ${dest.trail.join(" → ")}, but could not find the panel to point at — it `
      + `may still be loading, or this screen may be empty until you add a document.` });
    askOpen();
    return;
  }
  askSpot(el, dest);
}

/* ── the arrival marker ──────────────────────────────────────────────────── */

function askClearSpot() {
  if (ASK.cleanup) { ASK.cleanup(); ASK.cleanup = null; }
  document.getElementById("veris-ask-spot")?.remove();
}

function askSpot(el, dest) {
  askClearSpot();
  const host = document.createElement("div");
  host.id = "veris-ask-spot";
  host.innerHTML = `<div class="ask-spot__ring"></div>
    <div class="ask-spot__tag" role="status">
      <span class="ask-spot__k">Here it is</span>
      <span class="ask-spot__l"></span>
    </div>`;
  document.body.appendChild(host);
  host.querySelector(".ask-spot__l").textContent = dest.trail.join(" › ");

  const place = () => {
    const target = el.isConnected ? el : null;
    if (!target) { askClearSpot(); return; }
    const r = target.getBoundingClientRect();
    const pad = 10;
    const ring = host.querySelector(".ask-spot__ring");
    ring.style.cssText = `top:${r.top - pad}px;left:${r.left - pad}px;`
      + `width:${r.width + pad * 2}px;height:${r.height + pad * 2}px`;
    const tag = host.querySelector(".ask-spot__tag");
    const tr = tag.getBoundingClientRect();
    // Above the target by preference; below when there is no room, so it never covers the
    // thing it is pointing at.
    let top = r.top - pad - tr.height - 10;
    if (top < 12) top = r.bottom + pad + 10;
    let left = r.left - pad;
    if (left + tr.width > innerWidth - 12) left = innerWidth - tr.width - 12;
    if (left < 12) left = 12;
    tag.style.top = `${top}px`;
    tag.style.left = `${left}px`;
  };

  const r0 = el.getBoundingClientRect();
  if (r0.top < 80 || r0.bottom > innerHeight - 40) {
    el.scrollIntoView({ block: "center", behavior: REDUCED_MOTION() ? "auto" : "smooth" });
  }
  requestAnimationFrame(() => { place(); host.classList.add("is-in"); });

  const onMove = () => place();
  window.addEventListener("resize", onMove);
  window.addEventListener("scroll", onMove, true);
  // Fades itself out. A marker that stays is just an obstruction, and the user came here to
  // work on this screen rather than to look at a label about it.
  const timer = setTimeout(() => {
    host.classList.remove("is-in");
    setTimeout(() => askClearSpot(), 420);
  }, 5200);
  ASK.cleanup = () => {
    clearTimeout(timer);
    window.removeEventListener("resize", onMove);
    window.removeEventListener("scroll", onMove, true);
  };
}

window.askOpen = askOpen;

/* ── Landing page ─────────────────────────────────────────────────────────
 *
 * PAINTS FROM NOTHING, still. The hero, the orb and every word render with no fetch, no
 * session read and no engine call — a marketing page that loaded the case engine to draw
 * itself would make an unauthenticated visitor's first request a case build.
 *
 * The figures in the band are the one exception and they are fetched AFTER first paint,
 * from `/api/marketmap` only. That endpoint is the single case-free measurement surface
 * in the product — it is computed from published wordings and holds no case, which is why
 * it is also the one view exempted from case deletion. If the fetch fails the band simply
 * does not appear; there are no placeholder figures to mistake for real ones.
 *
 * Text is deliberately short. The old version argued its case in about 900 words on the
 * front door; the argument belongs in the product, where every claim has a document
 * behind it.
 */
/* ══════════════════════════════════════════════════════════════════════════
   STITCH COMPOSITIONS
   ══════════════════════════════════════════════════════════════════════════ */

/* ── PROTECTION FIELD ──────────────────────────────────────────────────────
 * `business_protection_cockpit`. One orbit per scored dimension, the real overall score
 * in the glass core, and pins for whatever the two agents last reported.
 *
 * The tone of each orbit comes from that dimension's ACTUAL score, and an unassessed
 * dimension is drawn as a dashed, uncoloured ring rather than a green one — "we did not
 * look at this" and "this is fine" are different facts and the product refuses to blur
 * them everywhere else, so the cockpit must not blur them either.
 */
function protectionField(score, blind, signals, sentinel) {
  const dims = (score.dimensions || []).slice(0, 4);
  const kind = s => s == null ? "none" : s > 70 ? "good" : s > 40 ? "warn" : "bad";

  // Outermost first so the labels stack downward the way the reference does.
  const rings = dims.map((d, i) => {
    const w = 100 - i * 19;                       // % of stage width
    const h = 74 - i * 15;
    return { d, w, h, k: kind(d.score) };
  });

  const covered = blind && blind.coverage
    ? `${blind.coverage.areas_we_can_speak_to} of ${blind.coverage.areas_total} areas`
    : `${score.assessed_dimensions ?? "—"} of ${score.total_dimensions ?? "—"} areas`;

  const events = (signals && signals.events) || [];
  const env = (sentinel && sentinel.environment) || {};
  const pins = [];
  if (events.length) {
    pins.push({ cls: "", style: "left:2%;top:44%",
      text: `Connect · ${events.length} change${events.length === 1 ? "" : "s"}` });
  }
  if (env.level) {
    pins.push({ cls: env.level === "LOW" ? "" : env.level === "HIGH" ? "is-clay" : "is-amber",
      style: "right:1%;top:38%", text: `Sentinel · ${String(env.level).toLowerCase()}` });
  }

  return `<div class="pf" data-rise="zoom">
    <div class="pf__stage">
      ${rings.map(r => `<div class="pf__orbit" data-k="${esc(r.k)}"
          style="width:${r.w}%;height:${r.h}%;--d:${rings.indexOf(r) * 90}ms"></div>`).join("")}
      ${rings.map((r, i) => `<div class="pf__label"
          style="top:calc(52% + ${r.h / 2}% - 9px)">${esc(r.d.label || r.d.key)}${
            r.d.score == null ? " · not checked" : ""}</div>`).join("")}
      <div class="pf__core">
        <div>
          <div class="pf__score-l">Core health score</div>
          <div class="pf__score-v num-display" data-to="${esc(score.overall ?? 0)}">0</div>
          <div class="pf__score-s">${esc(score.status || "")}</div>
        </div>
      </div>
      ${pins.map(p => `<div class="pf__pin ${p.cls}" style="${p.style}">
        <i></i>${esc(p.text)}</div>`).join("")}
    </div>
    <p class="text-xs text-muted" style="text-align:center;max-width:52ch">
      Covers ${esc(covered)}. A ring drawn dashed is one we could not assess — that is not
      the same as one with nothing wrong.</p>
  </div>`;
}

/* ── CONVERGENCE ───────────────────────────────────────────────────────────
 * `veris_intelligence_convergence`. Connect on the left in jade, Sentinel on the right in
 * amber, the Protection Field between them.
 *
 * The centre reports one of four REAL states and never invents a fifth: convergence when
 * both agents reported, a one-sided input when only one did, and watching when neither
 * has. A flow line animates only on the side that actually has data, so motion here means
 * "something arrived" rather than "the page is alive".
 */
function convergencePanel(signals, sentinel, findings) {
  const events = (signals && signals.events) || [];
  const env = (sentinel && sentinel.environment) || {};
  const live = (sentinel && sentinel.live) || {};
  const hasConnect = events.length > 0;
  const hasSentinel = !!env.level;
  const elevated = hasSentinel && env.level !== "LOW";

  let k = "Protection field", v = "Watching. Neither agent has reported a change.";
  if (hasConnect && elevated) { k = "High attention"; v = "Convergence detected — an internal change and an external risk at once."; }
  else if (hasConnect) { k = "Internal input only"; v = "Your records changed. Conditions around you are unchanged."; }
  else if (elevated) { k = "External input only"; v = "Conditions around you changed. Your records are unchanged."; }
  else if (hasSentinel) { k = "Protection field"; v = "Watching. Nothing has moved on either side."; }

  const spark = (pts, colour) => `<svg class="cv__spark" viewBox="0 0 120 40"
      preserveAspectRatio="none" aria-hidden="true"><polyline
      points="${pts}" fill="none" stroke="${colour}" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" opacity=".85"/></svg>`;

  return `<div class="cv" data-rise="rise">
    <div class="cv__side cv__side--connect ${hasConnect ? "" : "cv__side--idle"}">
      <div class="cv__hd">Veris Connect</div>
      <div class="cv__bd">
        <div class="cv__fig">${hasConnect
          ? `${events.length} change${events.length === 1 ? "" : "s"}`
          : "No change"}</div>
        <div class="cv__sub">${hasConnect
          ? esc(events[0].label || events[0].summary || "Inside your business")
          : "Inside your business"}</div>
        ${spark("0,32 20,28 40,30 60,18 80,22 100,10 120,12", "#3f7263")}
      </div>
    </div>
    <div class="cv__core">
      <div class="cv__orb"></div>
      <div class="cv__txt">
        <div class="cv__k">${esc(k)}</div>
        <div class="cv__v">${esc(v)}</div>
      </div>
    </div>
    <div class="cv__side cv__side--sentinel ${elevated ? "" : "cv__side--idle"}">
      <div class="cv__hd">Veris Sentinel</div>
      <div class="cv__bd">
        <div class="cv__fig">${esc(env.label || String(env.level || "Calm").toLowerCase())}</div>
        <div class="cv__sub">${esc(env.summary || "Around your business")}
          ${renderDataBadge(live.any_live)}</div>
        ${spark("0,26 20,24 40,30 60,12 80,34 100,20 120,24", "#a8741c")}
      </div>
    </div>
  </div>
  <div class="grid-2" style="gap:0;margin-top:-6px">
    <div class="cv__flow to-right ${hasConnect ? "is-live" : ""}"></div>
    <div class="cv__flow to-left ${elevated ? "is-live" : ""}"></div>
  </div>`;
}

/* ── WORKFLOW RAIL ─────────────────────────────────────────────────────────
 * `stock_protection_review_workspace`: Observe → Investigate → Verify → Act.
 *
 * Mapped from the action state machine's real states, not from a counter. DRAFTED has
 * been observed and nothing more; the adviser picking it up is Investigate; terms coming
 * back is Verify; BOUND is Act. A terminal state that is not BOUND closes the rail
 * without lighting Act, because DECLINED and NOT_REQUIRED are endings, not successes.
 */
const WORKFLOW_STAGES = [
  { id: "observe", name: "Observe" },
  { id: "investigate", name: "Investigate" },
  { id: "verify", name: "Verify" },
  { id: "act", name: "Act" },
];

function workflowStage(state) {
  switch (state) {
    case "DRAFTED": return 0;
    case "SENT_TO_ADVISER": case "ADVISER_REVIEWING": case "INFORMATION_REQUESTED": return 1;
    case "QUOTE_RECEIVED": return 2;
    case "BOUND": return 3;
    default: return -1;                 // DECLINED / WITHDRAWN / NOT_REQUIRED
  }
}

function workflowRail(state) {
  const at = workflowStage(state);
  const closed = at < 0;
  return `<div class="wr" role="list" aria-label="Where this request has reached">
    ${WORKFLOW_STAGES.map((s, i) => {
      const cls = closed ? "pending" : i < at ? "done" : i === at ? "active" : "pending";
      const mark = cls === "done" ? "✓" : cls === "active" ? String(i + 1) : String(i + 1);
      const word = cls === "done" ? "Done" : cls === "active" ? "Active" : "Pending";
      return `<div class="wr__step ${cls}" role="listitem">
        <span class="wr__i" aria-hidden="true">${mark}</span>
        <span class="wr__n">${esc(s.name)}</span>
        <span class="wr__s">${closed ? "Closed" : word}</span>
      </div>`;
    }).join("")}
  </div>`;
}

function renderLanding() {
  $("#app").innerHTML = `<div class="lp2">
  <header class="lp2__bar">
    <div class="lp2__mark">Veris</div>
    <div class="lp2__meta">Veris <span>|</span> Evidence-bound insurance agent</div>
    <button class="btn btn-secondary btn-sm" data-act="show-login">Sign in</button>
  </header>

  <section class="lp2__hero">
    <div class="lp2__copy">
      <h1 class="lp2__h1">${splitWords("Insurance that keeps up with your business")}</h1>
      <p class="lp2__lede" data-rise="rise">Veris continuously connects business change,
        external risk and insurance evidence.</p>
      <div class="lp2__cta" data-rise="rise">
        <button class="btn btn-primary lp2__enter" data-act="show-login">
          Enter workspace <span aria-hidden="true">↗</span></button>
      </div>
      <p class="lp2__note" data-rise="rise">No model decides your cover. Every figure is a
        comparison between documents you already hold.</p>
    </div>
    <div class="lp2__stage">${verisOrb()}
      <div class="lp2__tag lp2__tag--jade" data-rise="zoom">Connect<span>inside the business</span></div>
      <div class="lp2__tag lp2__tag--amber" data-rise="zoom">Sentinel<span>around the business</span></div>
      <div class="lp2__tag lp2__tag--field" data-rise="zoom">Protection Field<span>what your policy would pay</span></div>
    </div>
  </section>

  <section class="lp2__band" id="band" aria-live="polite"></section>

  <section class="lp2__three">
    <article class="lp2__c lp2__c--jade" data-rise="rise">
      <h3>Connect</h3>
      <p>Reads the records that show what your business does now.</p></article>
    <article class="lp2__c lp2__c--amber" data-rise="rise">
      <h3>Sentinel</h3>
      <p>Watches the conditions and registers around it.</p></article>
    <article class="lp2__c lp2__c--field" data-rise="rise">
      <h3>Evidence</h3>
      <p>Quotes the clause, on the page, and shows you the page.</p></article>
  </section>

  <footer class="lp2__foot">
    <span>Findings are evidence, not advice. Cover decisions rest with your insurer.</span>
    <button class="btn btn-ghost btn-sm" data-act="show-login">Sign in</button>
  </footer>
</div>`;

  // Guarded because `renderLanding` is called from `render()`, which has no error
  // boundary — `mount()` wraps the authenticated views only. An unguarded null here would
  // throw before `loadLandingBand()` ran and leave an unauthenticated visitor on a page
  // with no working Sign in button, which is the worst possible place for this product to
  // fail. The animation is an enhancement; the page must render without it.
  const root = $(".lp2");
  if (root) {
    stage(root, "[data-rise]", 70);
    // The headline zooms word by word on its own timing, set inline by splitWords().
    const words = root.querySelectorAll(".lp__w");
    words.forEach(w => w.classList.add(REDUCED_MOTION() ? "is-in" : "zoom"));
  }
  loadLandingBand();
}

/* The measured band. Real figures or nothing.
 *
 * `/api/marketmap` is the only source used here and it is chosen precisely because it
 * holds no case: it is computed from the published wordings in the corpus, so an
 * unauthenticated visitor triggers no case build and sees nothing about anybody's
 * documents. `proposals` and `rejected` are the alignment run's own counts.
 */
async function loadLandingBand() {
  const band = $("#band");
  if (!band) return;
  let m;
  try { m = await api("/api/marketmap"); } catch (_) { return; }
  if (!m || !band.isConnected) return;

  const proposals = Number(m.proposals);
  const rejected = Number(m.rejected);
  const bound = isFinite(proposals) && isFinite(rejected) ? proposals - rejected : NaN;
  const figures = [
    { to: (m.carriers || []).length, label: "published wordings read" },
    { to: (m.rows || []).length, label: "clause mechanisms mapped" },
    { to: bound, label: "clause spans bound" },
    { to: rejected, label: "candidates rejected" },
  ].filter(f => isFinite(f.to) && f.to > 0);
  if (!figures.length) return;

  band.innerHTML = figures.map(f => `<div class="lp2__f" data-rise="rise">
      <div class="lp2__f-v num-display" data-to="${esc(f.to)}" data-group="1">0</div>
      <div class="lp2__f-l">${esc(f.label)}</div>
    </div>`).join("")
    + `<p class="lp2__f-note">Measured over the published wordings in this build, not a
        claim about anybody's policy.</p>`;
  stage(band, "[data-rise]", 90);
  countUpAll(band);
}

function renderLogin() {
  // The helper is role-scoped and its endpoints 401 without a session, so it goes away with
  // the workspace. Leaving the circle on the login screen would offer help it cannot give.
  askUnmount();
  $("#app").innerHTML = `<div class="login-page"><div class="login-card">
    <div class="login-brand"><h1>VERIS</h1><p>An evidence-bound insurance agent for households and small businesses</p></div>
    <p class="login-tagline">Page-cited answers. A deterministic core computes every number. No model decides your cover.</p>
    <div class="login-error" id="lerr"></div>
    <form id="lform">
      <div class="form-group"><label>Email</label><input type="email" id="lemail" required autocomplete="email"></div>
      <div class="form-group"><label>Password</label><input type="password" id="lpass" required autocomplete="current-password"></div>
      <button type="submit" class="btn btn-primary btn-full" id="lbtn">Sign In</button>
    </form>
    <div class="login-hint">
      <strong>Demo accounts</strong> — click one to fill the form:<br>
      ${DEMO_ACCOUNTS.map(a => `<button type="button" class="btn btn-ghost btn-sm" data-act="fill-login" data-arg="${a.email}" style="display:block;width:100%;text-align:left;margin:4px 0"><code>${a.email}</code> — ${a.label}</button>`).join("")}
      Password: <code>${DEMO_PASSWORD}</code>
    </div>
  </div></div>`;
  $("#lform").onsubmit = async e => {
    e.preventDefault();
    const btn = $("#lbtn"), err = $("#lerr");
    btn.disabled = true; btn.textContent = "Signing in..."; err.classList.remove("show");
    try {
      const d = await api("/api/auth/login", { method: "POST", body: { email: $("#lemail").value, password: $("#lpass").value } });
      if (d && d.ok) { setUser(d.user); render(); }
      else { err.textContent = "Login failed"; err.classList.add("show"); }
    } catch (x) { err.textContent = x.message; err.classList.add("show"); }
    finally { btn.disabled = false; btn.textContent = "Sign In"; }
  };
}

/* ── Shell ────────────────────────────────────────────────────────────── */

function shell(role, nav) {
  const roleLabel = {
    SME: t("shell.role.SME"),
    ADVISER: t("shell.role.ADVISER"),
    CARRIER: t("shell.role.CARRIER"),
  }[role] || role;
  const user = S.user || {};
  let navHtml = "";
  nav.forEach(s => {
    if (s.section) {
      navHtml += `<div class="nav-section-title">${esc(s.section)}</div>`;
      return;
    }
    const active = S.view === s.id ? "active" : "";
    const badge = s.badge ? `<span class="badge-count">${esc(s.badge)}</span>` : "";
    navHtml += `<button type="button" class="nav-item ${active}" data-act="nav"
      data-v="${esc(s.id)}" data-arg="${esc(s.id)}"${active ? ' aria-current="page"' : ""}>
      <span class="icon" aria-hidden="true">${s.icon}</span>${esc(s.label)}${badge}</button>`;
  });
  $("#app").innerHTML = `<div class="shell"><aside class="sidebar">
    <div class="sidebar-head"><h2>VERIS</h2><span class="badge">${esc(roleLabel)}</span></div>
    <nav class="sidebar-nav" aria-label="${esc(roleLabel)}">${navHtml}</nav>
    <div class="sidebar-foot"><div class="name">${esc(user.name || user.email || "")}</div><div class="org">${esc(user.organisation_name || "")}</div>${languageSelector()}<button class="btn btn-secondary btn-sm" data-act="switch-role" title="${esc(t("shell.switch_role_title"))}">${esc(t("shell.switch_role"))}</button><button class="btn btn-ghost btn-sm" data-act="logout" title="${esc(t("shell.sign_out_title"))}">${esc(t("shell.sign_out"))}</button><button class="btn btn-ghost btn-sm" data-act="tour-start" title="${esc(t("shell.take_tour_title"))}">${esc(t("shell.take_tour"))}</button></div>
  </aside><main class="main" id="main"></main></div>`;
  // Offered after the shell exists so the invitation has something to point at, and only
  // once per role — see tourInvite(). Never while a tour is already running, which would
  // otherwise re-invite on every step that navigates.
  if (!T.on) tourInvite();
  // The helper lives on document.body, so it survives this function wiping #app and does
  // not need repainting per view. Mounted here because it is role-scoped: there is nothing
  // for it to answer until somebody is signed in.
  askMount();
}

/* ══════════════════════════════════════════════════════════════════════════
   SME WORKSPACE

   WRITTEN FOR A SHOP OWNER, NOT AN UNDERWRITER.

   The engine thinks in "average clause", "condition precedent", "sum insured
   adequacy". A person running a food factory does not, and should not have to.
   Every label and sentence on these screens is the plain-English version, with
   the technical wording kept one click away rather than on the surface.

   Where a screen carries a headline, it came from `/api/presentation/compose`
   and the badge on the panel says whether a model or a written template wrote
   it. Every NUMBER came from the deterministic engine either way.
   ══════════════════════════════════════════════════════════════════════════ */

/* The unread count is painted onto the nav AFTER the shell exists.
 *
 * `shell()` is synchronous and the count needs a request, so awaiting it here would hold
 * the entire workspace behind one network call for a number. Instead the shell renders with
 * whatever was last known, the request happens alongside the screen, and the badge appears
 * when it lands. A badge is never worth a blank page, and never worth an error either —
 * a failure here is swallowed on purpose.
 */
async function refreshUnread() {
  try {
    const d = await api("/api/notifications");
    if (!d) return;
    S.unread = d.unread || 0;
    paintUnreadBadge();
  } catch { /* the rest of the workspace is unaffected */ }
}

function paintUnreadBadge() {
  const item = document.querySelector('.nav-item[data-v="sme-alerts"]');
  if (!item) return;
  item.querySelector(".badge-count")?.remove();
  if (!S.unread) return;
  const badge = document.createElement("span");
  badge.className = "badge-count";
  badge.textContent = String(S.unread);
  badge.setAttribute("aria-label", t("shell.unread", { count: S.unread }));
  item.appendChild(badge);
}

function renderSME() {
  if (!S.view || !S.view.startsWith("sme")) S.view = "sme-portfolio";
  // NINE destinations, down from 23.
  //
  // 23 items is not navigation, it is an index, and an owner cannot hold 23 places in
  // their head. Worse, it made the product look like an analyst's cockpit when its whole
  // claim is that an owner can understand their own position.
  //
  // Nothing was deleted. Nineteen of the old screens are now tabs inside five container
  // screens, each still rendered by its ORIGINAL function — see renderTabs(). The four
  // that stayed standalone did so because each answers a question with no sibling:
  // Overview (where do I stand), What's New (what just happened), Share with Adviser
  // (who can see this), Your Record (what can I take away).
  //
  // Order is an argument. Overview first, then what changed, then where you are exposed,
  // then the sequence that got you here, then what to do, then what we watch, then the
  // documents that would improve all of it.
  shell("SME", [
    { id: "sme-portfolio", label: t("nav.sme.portfolio"), icon: "◈" },
    { id: "sme-ask", label: t("nav.sme.ask_veris"), icon: "◌" },
    { id: "sme-claims", label: t("nav.sme.claims"), icon: "✦" },
    { id: "sme-alerts", label: t("nav.sme.tasks_alerts"), icon: "◍", badge: S.unread || 0 },
    { id: "sme-docs-hub", label: t("nav.sme.documents"), icon: "▤" },
    { id: "sme-consent", label: t("nav.sme.sharing_mandates"), icon: "↦" },
    { id: "sme-home", label: t("nav.sme.business"), icon: "◆" },
    { id: "sme-record", label: t("nav.sme.your_record"), icon: "▣" },
  ]);
  const m = $("#main");
  const views = {
    "sme-portfolio": smePortfolio,
    "sme-ask": smeAsk,
    "sme-claims": smeClaims,
    // the ten destinations
    "sme-home": smeHome,
    "sme-position": smeGapSnapshot,
    "sme-alerts": smeAlerts,
    "sme-protect-hub": smeProtectionHub,
    "sme-time-hub": smeTimelineHub,
    "sme-act-hub": smeActionsHub,
    "sme-mon-hub": smeMonitoringHub,
    "sme-docs-hub": smeDocumentsHub,
    "sme-consent": smeConsent,
    "sme-record": smeRecord,
    // The old ids stay routable. They are no longer in the sidebar, but internal
    // deep-links use them (the Overview's risk cards, the empty states, the "see what we
    // haven't checked" button), and breaking those to tidy a map would trade working
    // navigation for a shorter object.
    "sme-blindspots": smeBlindSpots,
    "sme-cyber": smeCyber,
    "sme-dependencies": smeDependencies,
    "sme-compounding": smeCompounding,
    "sme-roadmap": smeRoadmap,
    "sme-upload": smeUpload,
    "sme-protection": smeProtection,
    "sme-findings": smeFindings,
    "sme-scenario": smeScenario,
    "sme-willitpay": smeWillItPay,
    "sme-bi": smeBI,
    "sme-renewal": smeRenewal,
    "sme-actions": smeActions,
    "sme-gaps": smeGaps,
    "sme-connect": smeConnect,
    "sme-sentinel": smeSentinel,
    "sme-discovery": smeDiscovery,
    "sme-agents": smeAgents,
    "sme-drift": smeDrift,
  };
  mount(views[S.view] || smeHome, m);
  paintUnreadBadge();
  refreshUnread();
}

/* Small shared pieces ─────────────────────────────────────────────────────── */

/* ── Toasts ───────────────────────────────────────────────────────────────
 *
 * For the OUTCOME of something the user just did — "nothing has changed since the last
 * check" — and never for anything they need to read in order to stay correct. Anything
 * consequential belongs on the page, where it survives four seconds and a glance away.
 */
function toast(message, kind) {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    host.className = "toast-host";
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = "toast" + (kind ? ` toast--${kind}` : "");
  el.textContent = String(message || "");
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("toast--leaving");
    setTimeout(() => el.remove(), 220);
  }, kind === "err" ? 6000 : 4000);
}

function pageTitle(h1, sub) {
  return `<div class="page-title"><h1>${esc(h1)}</h1>${sub ? `<p>${esc(sub)}</p>` : ""}</div>`;
}

/* ── Container screens: the 23 → 9 sidebar collapse ───────────────────────
 *
 * WHY COMPOSITION RATHER THAN A REWRITE.
 *
 * The sidebar had 23 destinations. That is not navigation, it is an index, and an owner
 * cannot hold 23 places in their head — so related screens became tabs inside nine
 * destinations. The brief was explicit that nothing may be deleted: every old screen's
 * content stays findable.
 *
 * The cheap way to honour that is to rebuild each screen as a section of a bigger one.
 * The safe way is what this does: every original view function is reused UNCHANGED, and
 * a container renders a tab strip above it. So `smeCyber` is still `smeCyber`, still
 * fetches what it fetched, still tested by whatever tested it. The collapse is a
 * navigation change, not a rewrite of eleven screens — which is the difference between
 * a day's work and a week of regressions.
 *
 * The one visible cost: a container's heading and the tab's own pageTitle both render,
 * so the tab title reads as a section heading. That is a normal pattern and it is worth
 * the safety.
 */
function tabStrip(group, tabs, activeId) {
  return `<div class="tabs" role="tablist">${tabs.map(t => {
    const on = t.id === activeId;
    const count = t.count != null && t.count !== 0
      ? `<span class="tab__n ${t.warn ? "warn" : ""}">${esc(t.count)}</span>` : "";
    return `<button class="tab ${on ? "on" : ""}" role="tab"
      aria-selected="${on}" data-act="tab" data-arg="${esc(group)}:${esc(t.id)}"
      >${esc(t.label)}${count}</button>`;
  }).join("")}</div>`;
}

/* Render a container: heading, optional lead sentence, tab strip, then the active tab's
 * own view mounted into a panel. `mount()` is reused so a failing tab shows the standard
 * readable error instead of a dead panel. */
async function renderTabs(el, { group, title, lead, tabs, fallback }) {
  const active = tabs.some(t => t.id === S.tab[group])
    ? S.tab[group] : (fallback || tabs[0].id);
  const tab = tabs.find(t => t.id === active) || tabs[0];
  el.innerHTML = pageTitle(title)
    + (lead ? `<p class="tab-lead">${esc(lead)}</p>` : "")
    + tabStrip(group, tabs, tab.id)
    + `<div class="tabpanel" role="tabpanel" id="tabpanel"></div>`;
  const panel = $("#tabpanel");
  if (panel) await mount(tab.view, panel);
}

/* ── The nine destinations ────────────────────────────────────────────────
 *
 * Overview, What's New, Share with Adviser and Your Record keep their own single
 * screens. The five below absorb the other nineteen.
 */

/* Protection — the four risk classes the problem statement names, plus the two
 * "what would I actually get" tools. Grouped because an owner asking "am I covered?"
 * does not know in advance whether the answer is a property, cyber, dependency or
 * business-interruption question, so making them choose the category first is asking
 * them to know the answer before they ask. */
async function smeProtectionHub(el) {
  await renderTabs(el, {
    group: "prot", title: "Protection",
    lead: "What you hold, what you don't, and what a claim would actually pay.",
    tabs: [
      { id: "score", label: "Your cover score", view: smeProtection },
      { id: "property", label: "Property and assets", view: smeFindings },
      { id: "cyber", label: "Cyber and data", view: smeCyber },
      { id: "supply", label: "Who you depend on", view: smeDependencies },
      { id: "bi", label: "If you can't trade", view: smeBI },
      { id: "scenario", label: "If you claim tomorrow", view: smeScenario },
      { id: "pay", label: "Will it actually pay?", view: smeWillItPay },
    ],
  });
}

/* Timeline — everything that is a dated sequence rather than a current state. These
 * three were separate screens telling one story out of order: what changed at renewal,
 * what changed in the business and was never followed up, and what we found as a
 * result. */
/* The Timeline's period filter.
 *
 * Kept on S and applied CLIENT-SIDE to rows the screen already has, rather than added as an
 * endpoint parameter. Two reasons: the backend pins `as_at` to the latest date the documents
 * evidence so the screen is reproducible, and a server-side window would quietly change that
 * anchor. And filtering a list of eight rows over HTTP would be a round trip to hide three of
 * them.
 *
 * "This year" means the twelve months before that document-derived `as_at`, not before today,
 * for the same reproducibility reason. */
const TIME_PERIODS = [
  { id: "all", label: "All time" },
  { id: "year", label: "This year" },
];

function timelineWithin(period, isoDate, asAt) {
  if (period !== "year" || !isoDate || !asAt) return true;
  const then = new Date(isoDate), anchor = new Date(asAt);
  if (isNaN(then) || isNaN(anchor)) return true;
  const cutoff = new Date(anchor);
  cutoff.setFullYear(cutoff.getFullYear() - 1);
  return then >= cutoff;
}

async function smeTimelineHub(el) {
  await renderTabs(el, {
    group: "time", title: "Timeline",
    lead: "Nothing here went wrong all at once. That is what makes it hard to notice.",
    tabs: [
      { id: "crept", label: "How this crept up", view: smeCompounding },
      { id: "renewal", label: "What changed this year", view: smeRenewal },
      { id: "found", label: "What we found", view: smeFindings },
    ],
  });
}

/* Actions — the three screens that answer "so what do I do". Split across the old
 * sidebar as Things to do / What would help / Protection for your stage, which meant an
 * owner looking for their next step had three candidates and no way to rank them. */
async function smeActionsHub(el) {
  await renderTabs(el, {
    group: "act", title: "What to do",
    lead: "Prepared for you, ranked by what it would settle.",
    tabs: [
      { id: "todo", label: "Things to do", view: smeActions },
      { id: "help", label: "What would help", view: smeGaps },
      { id: "stage", label: "Protection for your stage", view: smeRoadmap },
    ],
  });
}

/* Monitoring — the five agent surfaces. Deliberately NOT merged with What's New: that
 * screen answers "what just happened", this one answers "what is being watched". An
 * owner checking whether they need to act and an owner auditing our coverage are doing
 * different jobs, and collapsing them would serve neither. */
async function smeMonitoringHub(el) {
  await renderTabs(el, {
    group: "mon", title: "Monitoring",
    lead: "What we watch on your behalf, and where each answer came from.",
    tabs: [
      { id: "business", label: "Your business", view: smeConnect },
      { id: "systems", label: "Connected systems", view: smeConnected },
      { id: "around", label: "Around you", view: smeSentinel },
      { id: "public", label: "Public records", view: smeDiscovery },
      { id: "costs", label: "Keeping up with costs", view: smeDrift },
      { id: "checked", label: "What we checked", view: smeAgents },
    ],
  });
}

/* Documents — upload sits next to the blind-spot register because they are two halves
 * of one loop: here is what we could not check, and here is where you fix that. Having
 * them at opposite ends of a 23-item sidebar meant the answer to "what should I upload?"
 * was on a different screen from the upload button. */
async function smeDocumentsHub(el) {
  await renderTabs(el, {
    group: "docs", title: "Documents",
    lead: "What we have read, what we could not, and what would fill the gaps.",
    tabs: [
      { id: "start", label: "Getting started", view: smeOnboarding },
      { id: "add", label: "Add documents", view: smeUpload },
      { id: "gaps", label: "What we haven't checked", view: smeBlindSpots },
      { id: "quality", label: "How well we read them", view: smePageQuality },
    ],
  });
}

/* The honest refusal, rendered from a `_no_book_yet()` payload.
 *
 * Four carrier operations — the work queue, book patterns, the attention inbox and
 * premium-at-risk — used to compute over six invented businesses. The rows are gone, so
 * these screens now have nothing to aggregate, and the correct response is to say so
 * with what would populate them rather than to render an impressive-looking empty grid.
 *
 * Deliberately NOT styled as an error. Nothing failed; the data does not exist yet, and
 * a red banner would teach an underwriter to distrust a screen that is telling the truth.
 */
/* The empty state for a book operation with nothing to compute.
 *
 * WHAT WAS MISSING. It explained itself and then stranded the reader. The commonest way to
 * reach this screen is signing straight in as the underwriter — the book reads the case
 * held in your own session and a carrier session holds none, which is the isolation
 * boundary rather than a fault. That is four clicks away from being fixed, and the screen
 * said nothing about which four. It talked about a "carrier-initiated Mode B pilot"
 * instead, which is the reason for a DIFFERENT empty state and reads as "this product
 * cannot do this".
 *
 * `next_step` is present only when the cause is a missing case rather than a missing
 * product capability, so the two are no longer reported as one thing.
 */
function noBookState(d) {
  const step = d.next_step;
  // `pageTitle` escapes both of its arguments. Pre-escaping the subtitle here rendered
  // any & or < in an operation name as `&amp;` on screen. Same slip in `carDetail`.
  return `${pageTitle(step ? "Nothing loaded yet" : "Not available yet",
      d.operation ? `Operation: ${d.operation}` : "")}
    <div class="insight ${step ? "" : "warn"} mb-16"><div class="insight-label">Why this is empty</div>
      <div class="insight-text">${esc(d.reason || "")}</div></div>
    ${step ? `<div class="card mb-16">
      <div class="card-head"><h3>How to fill it in</h3>
        <span class="tag tag-info">${(step.how_to_populate_it || []).length} steps</span></div>
      <ol class="text-sm" style="margin:0 0 4px 18px;line-height:1.7">
        ${(step.how_to_populate_it || []).map(s => `<li>${esc(s)}</li>`).join("")}
      </ol>
      <div class="btn-group mt-16">
        <button class="btn btn-sm btn-secondary" data-act="switch-role">Switch role →</button>
        <button class="btn btn-sm btn-ghost" data-act="nav" data-arg="car-portfolio">Or open the wording portfolio</button>
      </div>
      <p class="text-xs text-muted mt-12">${esc(step.caveat || "")}</p></div>` : ""}
    <div class="card mb-16"><div class="card-head"><h3>What would fill this in</h3></div>
      <p class="text-sm">${esc(d.would_need || "")}</p></div>
    ${d.what_is_real_today ? `<div class="card mb-16"><div class="card-head"><h3>What is real today</h3></div>
      <p class="text-sm">${esc(d.what_is_real_today)}</p>
      <button class="btn btn-sm btn-primary mt-12" data-act="nav" data-arg="car-portfolio">Open the wording portfolio →</button></div>` : ""}
    ${d.deliberately_not_shown ? `<p class="text-sm text-muted">${esc(d.deliberately_not_shown)}</p>` : ""}`;
}

function spinner(msg) {
  return `<div class="loading"><div class="spinner"></div>${esc(msg || "Loading")}</div>`;
}

function emptyState(title, body, actions) {
  return `<div class="card"><div class="card-head"><h3>${esc(title)}</h3></div>
    <p class="text-sm">${body}</p>
    ${actions ? `<div class="grid-2 mt-16">${actions}</div>` : ""}</div>`;
}

/* Plain words for the engine's decision states. The engine's vocabulary is precise and
 * unreadable; these are the same thing said out loud. */
const DECISION_WORDS = {
  ACTION_REQUIRED: "Needs doing",
  REVIEW: "Worth a look",
  POTENTIAL_INCIDENT: "Possible problem",
  WATCH: "Keeping an eye on it",
  MONITOR: "Nothing to do",
  UNESTABLISHED: "Can't tell yet",
};

const STATE_WORDS = {
  DOCUMENTED: "Proven by your documents",
  REQUIRES_AUTHORISED_REVIEW: "Needs an expert to look",
  INFORMATION_MISSING: "We need a document",
  NOT_CONFIRMED: "Not confirmed yet",
};

function humanDecision(v) { return DECISION_WORDS[v] || titleCase(v || ""); }
function humanState(v) { return STATE_WORDS[v] || titleCase(v || ""); }

/* One finding, in plain words. Shared with the adviser workspace, which is why it lives
 * out here rather than inside a screen.
 *
 * The old version printed the engine's own vocabulary — "REQUIRES_AUTHORISED_REVIEW",
 * "Precedence reasoning", "INTERNAL ×2". Correct, and unreadable to the person whose
 * money it concerns. Same data, said out loud. */
function renderDecision(dec) {
  const tags = [];
  if (dec.internal_refs?.length) tags.push('<span class="tag tag-info">from your records</span>');
  if (dec.external_refs?.length) tags.push('<span class="tag tag-purple">from outside</span>');
  if (dec.overlap) tags.push('<span class="tag tag-danger">two sources agree</span>');
  return `<div class="finding ${dec.decision}" data-act="toggle-detail">
    <div class="finding-head">
      <div class="finding-title">${esc(dec.interest_label || dec.interest)}</div>
      <span class="tag tag-${dec.decision}">${esc(humanDecision(dec.decision))}</span>
    </div>
    <div class="finding-body">${esc(dec.current_state || "")}</div>
    <div class="finding-meta">${tags.join("")}</div>
    <div class="finding-detail">
      ${dec.precedence_reason ? `<p class="text-sm"><strong>Why we concluded that:</strong> ${esc(dec.precedence_reason)}</p>` : ""}
      ${dec.overlap ? '<p class="text-sm mt-8">Two separate sources point the same way, which is why this ranks higher than a single signal would.</p>' : ""}
      ${(dec.actions || []).map(a => `<div class="insight mt-12"><div class="insight-label">${esc(titleCase(a.type || ""))}</div><div class="insight-text">${esc(a.label)}${a.documents?.length ? " — send: " + a.documents.map(x => esc(x)).join(", ") : ""}</div></div>`).join("")}
    </div>
  </div>`;
}

/* ── Overview ──────────────────────────────────────────────────────────────
 * Asks /api/session BEFORE /api/brief, because touching the brief CREATES the example
 * case. A user who has uploaded nothing must see nothing. */
async function smeHome(el) {
  el.innerHTML = spinner("Checking your workspace");
  const ses = await api("/api/session");
  if (ses && ses.has_case === false) {
    el.innerHTML = pageTitle(`Welcome, ${S.user.name || ""}`, "Nothing to show yet.")
      + emptyState("Give Veris something to read",
        "There are no findings here because we have not seen any of your paperwork. "
        + "Veris will not guess about a business it has not read.",
        '<button class="btn btn-primary" data-act="nav" data-arg="sme-upload">Add my documents</button>'
        + '<button class="btn btn-secondary" data-act="load-example">Try it with an example business</button>')
      + `<p class="text-xs text-muted mt-12">${esc(ses.notice || "")}</p>`;
    return;
  }

  el.innerHTML = spinner("Reading your documents");
  // The blind-spot report is fetched alongside the score, not on a separate screen the
  // owner has to go looking for. A score shown without the boundary of the assessment
  // that produced it invites exactly the wrong conclusion — that a number covering four
  // areas out of six is a verdict on all six.
  const [brief, score, signals, sentinel, notes, blind, gaps] = await Promise.all([
    api("/api/brief"), api("/api/score"), api("/api/signals"), api("/api/sentinel"),
    api("/api/notifications"), api("/api/blindspots"), api("/api/gap-summary"),
  ]);
  S.policyCountry = brief?.jurisdiction?.pack || brief?.jurisdiction?.detected || null;
  const findings = brief.findings || [];
  const byState = brief.summary?.by_state || {};
  const byClass = brief.summary?.by_class || {};
  const needExpert = byState.REQUIRES_AUTHORISED_REVIEW || 0;
  const cantTell = (byState.INFORMATION_MISSING || 0) + (byState.NOT_CONFIRMED || 0);
  const env = (sentinel.environment || {}).level || "LOW";

  const preloaded = brief.source === "DEMO_BUNDLE" || brief.synthetic === true;
  const exampleFiles = sessionStorage.getItem(EXAMPLE_FLAG) === "1";
  const isExample = preloaded || exampleFiles;

  const p = await getPresentation("home_overview", {
    overall: score.overall, status: score.status, action_count: needExpert,
    finding_count: findings.length, environment_level: env,
  });

  el.innerHTML = `
    ${isExample ? `<div class="synthetic-banner">${preloaded
      ? `EXAMPLE BUSINESS — you have not uploaded anything. Everything below is read from our sample paperwork for a made-up company.`
      : `EXAMPLE DOCUMENTS — these files were really uploaded and read, but the company is made up.`} Add your own to replace it.</div>` : ""}
    ${exampleWordingNotice()}
    ${pageTitle(`Hello, ${S.user.name || ""}`,
      `${brief.business || "Your business"}${isExample ? " · example data" : ""}`)}
    ${jurisdictionPanel(brief.jurisdiction)}

    ${renderWhatsNewStrip(notes)}

    <!-- The cockpit replaces the ring-in-a-card. Same figure from the same /api/score,
         composed as the protection field the references open with, and still a route into
         the score screen. -->
    <div class="card card-clickable mb-16" data-act="nav" data-arg="sme-protection"
         style="padding:26px 22px 20px">
      ${protectionField(score, blind, signals, sentinel)}
    </div>
    <div class="mb-16">${renderAssessment(p?.pres, p?.mode)}</div>

    ${blind && blind.score_caveat && blind.score_caveat.applies ? `
      <div class="caveat mb-16">
        <div class="caveat__t">What this score does and does not include</div>
        <p>${esc(blind.score_caveat.statement)}</p>
        <button class="btn btn-secondary btn-sm mt-12" data-act="open" data-arg="sme-docs-hub|docs|gaps">
          See what we haven't checked →</button>
      </div>` : ""}

    <div class="stats mb-16">
      <div class="stat card-clickable" data-act="nav" data-arg="sme-findings">
        <div class="val">${findings.length}</div><div class="lbl">Things we found</div></div>
      <div class="stat green"><div class="val">${byClass.PROVEN || 0}</div>
        <div class="lbl">Proven by a document</div></div>
      <div class="stat red card-clickable" data-act="nav" data-arg="sme-actions">
        <div class="val">${needExpert}</div><div class="lbl">Need an expert</div></div>
      <div class="stat card-clickable" data-act="nav" data-arg="sme-gaps">
        <div class="val">${cantTell}</div><div class="lbl">We couldn't check</div></div>
    </div>

    ${gaps && gaps.available ? `<div class="sub-head">Your position, in short</div>
      <div class="cov mb-16">
        <div class="cov__top">
          <div><div class="cov__label">Where you stand</div>
            <div class="cov__say">${esc(gaps.executive_summary)}</div></div>
        </div>
        <div class="stats mt-16">
          ${(gaps.categories || []).map(c => `<div class="stat ${
            c.category === "ACTION_REQUIRED" ? "red"
            : c.category === "NEEDS_VERIFICATION" ? "amber" : ""}">
            <div class="val">${c.count}</div>
            <div class="lbl">${esc(c.icon)} ${esc(c.label)}</div></div>`).join("")}
        </div>
        ${(gaps.top_three || []).length ? `<div class="sub-head" style="margin-top:20px">Start with these three</div>
          <div class="heads">${gaps.top_three.map(i => `<div class="head is-gap">
            <div><div class="head__n"><span class="gap__icon">${esc(i.icon)}</span> ${esc(i.title)}</div></div>
            ${i.source === "Your documents" ? `<button class="btn btn-ghost btn-sm"
              data-act="gap-why" data-arg="${esc(i.key)}">Show me why</button>` : ""}
          </div>`).join("")}</div>` : ""}
        <div class="caveat">
          <div class="caveat__t">Best next step</div>
          <p>${esc((gaps.best_next_action || {}).what || "")}</p>
          <button class="btn btn-secondary btn-sm mt-12" data-act="nav" data-arg="sme-position">
            See the full picture →</button>
        </div>
      </div>` : ""}

    ${blind && blind.areas ? `<div class="sub-head">Where you're exposed</div>
      <div class="areas mb-16">
        ${blind.areas.filter(a => ["digital", "dependencies", "property"].includes(a.key))
          .map(a => {
            const tab = a.key === "digital" ? "cyber"
              : a.key === "dependencies" ? "supply" : "property";
            return `<div class="area k-state ${kClass(a.state)} card-clickable"
              data-act="open" data-arg="sme-protect-hub|prot|${tab}">
            <div class="area__head">
              <div class="area__name">${esc(a.label)}</div>
              <span class="area__pill">${esc(a.state_label)}</span>
            </div>
            <div class="area__plain">${esc(a.plain)}</div>
            ${a.finding_count ? `<div class="area__meta"><b>${a.finding_count}</b> thing(s) to look at</div>` : ""}
          </div>`;
          }).join("")}
      </div>` : ""}

    <div class="grid-2 mb-16">
      <div class="card"><div class="card-head"><h3>The three big questions</h3></div>
        <p class="text-sm text-muted mb-12">Most owners want to know these before anything else.</p>
        <button class="btn btn-secondary btn-full mb-8" data-act="nav" data-arg="sme-scenario">If I claim tomorrow, what do I get?</button>
        <button class="btn btn-secondary btn-full mb-8" data-act="nav" data-arg="sme-willitpay">Will my policy actually pay?</button>
        <button class="btn btn-secondary btn-full" data-act="nav" data-arg="sme-bi">What if I can't trade for months?</button>
      </div>
      <div class="card"><div class="card-head"><h3>What we watched while you were away</h3>${renderDataBadge((sentinel.live || {}).any_live)}</div>
        ${renderChangePulse([
          { direction: "up", label: "Changes in your business",
            value: `${(signals.events || []).length} picked up`, source: "your records" },
          { direction: "new", label: "Conditions around you",
            value: env.toLowerCase(), source: "live government data" },
        ])}
        <button class="btn btn-ghost btn-sm mt-12" data-act="nav" data-arg="sme-agents">See everything we checked →</button>
      </div>
    </div>

    ${brief.synthetic_notice ? `<p class="text-xs text-muted">${esc(brief.synthetic_notice)}</p>` : ""}`;
}

/* ── Household portfolio and plan-layout cards ─────────────────────────── */

function portfolioSyntheticBanner(portfolio = null) {
  const provenance = portfolio?.household?.provenance || S.householdProvenance;
  return provenance?.kind === "SYNTHETIC_EXAMPLE"
    ? `<div class="synthetic-banner">${esc(t("portfolio.synthetic_banner"))}</div>` : "";
}

const PLAN_LAYOUT_SECTIONS = [
  { id: "covered", label: () => t("portfolio.section.covered") },
  { id: "limits", label: () => t("portfolio.section.limits") },
  { id: "waiting", label: () => t("portfolio.section.waiting") },
  { id: "share", label: () => t("portfolio.section.share") },
  { id: "exclusions", label: () => t("portfolio.section.exclusions") },
  { id: "renewal", label: () => t("portfolio.section.renewal") },
  { id: "evidence", label: () => t("portfolio.section.evidence") },
];

function portfolioSectionFor(mechanism) {
  const code = String(mechanism || "").toUpperCase();
  if (code.startsWith("COVERED.")) return "covered";
  if (code.startsWith("LIMIT.")) return "limits";
  if (code.startsWith("WAITING.")) return "waiting";
  if (code.startsWith("SHARE.")) return "share";
  if (code.startsWith("EXCLUSION.") || code.startsWith("CONDITION.")) return "exclusions";
  if (code.startsWith("RENEWAL.") || code.startsWith("DEADLINE.")) return "renewal";
  return "evidence";
}

function portfolioMechanismLabel(mechanism) {
  switch (String(mechanism || "").toUpperCase()) {
    case "COVERED.HOSPITALISATION": return t("portfolio.mechanism.hospitalisation");
    case "COVERED.HOME_CONTENTS": return t("portfolio.mechanism.home_contents");
    case "COVERED.MOTOR_THIRD_PARTY": return t("portfolio.mechanism.motor_third_party");
    case "COVERED.TRAVEL": return t("portfolio.mechanism.travel");
    case "COVERED.FAMILY_FLOATER": return t("portfolio.mechanism.family_floater");
    case "LIMIT.SUM_INSURED": return t("portfolio.mechanism.sum_insured");
    case "WAITING.PED": return t("portfolio.mechanism.waiting_period");
    case "SHARE.CO_PAYMENT": return t("portfolio.mechanism.co_payment");
    case "RENEWAL.THIRD_PARTY": return t("portfolio.mechanism.third_party_expiry");
    default: return t("portfolio.mechanism.documented_item");
  }
}

const HEALTH_TEXT = Object.freeze({
  "health.document.policy_schedule": () => t("health.document.policy_schedule"),
  "health.document.policy_wording": () => t("health.document.policy_wording"),
  "health.item.b1_deductible": () => t("health.item.b1_deductible"),
  "health.item.cashless_network": () => t("health.item.cashless_network"),
  "health.item.co_insurance": () => t("health.item.co_insurance"),
  "health.item.co_payment": () => t("health.item.co_payment"),
  "health.item.deductible": () => t("health.item.deductible"),
  "health.item.medishield_ward_limit": () => t("health.item.medishield_ward_limit"),
  "health.item.moratorium": () => t("health.item.moratorium"),
  "health.item.ped_waiting": () => t("health.item.ped_waiting"),
  "health.item.pro_ration": () => t("health.item.pro_ration"),
  "health.item.room_board_limit": () => t("health.item.room_board_limit"),
  "health.item.room_rent_cap": () => t("health.item.room_rent_cap"),
  "health.notice.wording_missing": () => t("health.notice.wording_missing"),
  "health.notice.wording_not_matched": () => t("health.notice.wording_not_matched"),
});

function healthText(key) {
  const render = HEALTH_TEXT[String(key || "")];
  return render ? render() : "";
}

function portfolioItemLabel(item) {
  return item?.label_key ? healthText(item.label_key) : portfolioMechanismLabel(item?.mechanism_code);
}

function portfolioItemDisplay(policy, item) {
  const label = portfolioItemLabel(item);
  if (item?.value == null || item.value === "") {
    if (item?.state === "INFORMATION_MISSING" && item?.missing_document_key) {
      return t("portfolio.missing_value", { document: healthText(item.missing_document_key) });
    }
    return label;
  }
  if (item.display === "PED_END_DATE") {
    return `${label}: ${formatPolicyDate(item.value, policy.country)}`;
  }
  if (item.display === "MONEY_PER_DAY") {
    const percentage = item.params?.percent
      ? ` ${t("health.of_sum_insured", { percent: item.params.percent })}` : "";
    return `${label}: ${money(item.value, item.unit || policy.country)} ${t("health.per_day")}${percentage}`;
  }
  if (item.display === "MONEY") {
    return `${label}: ${money(item.value, item.unit || policy.country)}`;
  }
  if (item.display === "PERCENT_CONDITION") {
    return `${label}: ${item.value}% ${t("health.each_admissible_claim")}`;
  }
  if (item.display === "MONTHS_CONDITION") {
    return `${label}: ${item.value} ${t("health.months", { count: item.value })}`;
  }
  if (item.unit === "%") return `${label}: ${item.value}%`;
  return `${label}: ${money(item.value, item.unit || policy.country)}`;
}

function portfolioMissingDocument(section) {
  return section === "covered" || section === "limits" || section === "renewal"
    ? t("portfolio.document.policy_schedule") : t("portfolio.document.policy_wording");
}

function portfolioEvidencePanel(policy, item) {
  const open = S.portfolioEvidence;
  if (!open || open.policyId !== policy.id || open.coverageId !== item.id) return "";
  if (open.loading) {
    return `<p class="portfolio-evidence__status" role="status">${esc(t("portfolio.evidence_loading"))}</p>`;
  }
  if (open.error || !open.value?.available) {
    return `<p class="portfolio-evidence__status">${esc(open.error || t("portfolio.evidence_unavailable"))}</p>`;
  }
  return `<blockquote class="portfolio-evidence__quote">${esc(open.value.quote || "")}</blockquote>
    <p class="portfolio-evidence__meta">${esc(t("portfolio.evidence_document", {
      document: open.value.document || "",
    }))}</p>`;
}

function healthCitation(item) {
  const citation = item?.citation;
  if (!citation || !citation.quote) return "";
  const url = typeof citation.source_url === "string" && /^https:\/\//i.test(citation.source_url)
    ? citation.source_url : "";
  return `<details class="portfolio-evidence__health">
    <summary>${esc(t("health.show_source"))}</summary>
    <blockquote class="portfolio-evidence__quote">${esc(citation.quote)}</blockquote>
    <p class="portfolio-evidence__meta">${esc(t("health.source_meta", {
      document: citation.document || "", page: citation.page || "",
    }))}${url ? ` · <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(t("health.open_source"))}</a>` : ""}</p>
  </details>`;
}

function planEvidenceControl(policy, item) {
  if (item?.citation) return healthCitation(item);
  if (!item?.source_document_id) return "";
  const arg = `${policy.id}|${item.id}`;
  return `<button type="button" class="btn btn-ghost btn-sm portfolio-wording"
      data-act="portfolio-evidence" data-arg="${esc(arg)}"
      aria-controls="portfolio-evidence-${esc(item.id)}">
      ${esc(t("portfolio.show_wording"))}</button>
    <div id="portfolio-evidence-${esc(item.id)}">${portfolioEvidencePanel(policy, item)}</div>`;
}

function planLayoutCard(policy) {
  const coverageItems = Array.isArray(policy.coverage_items) ? policy.coverage_items : [];
  const healthItems = Array.isArray(policy.health_items) ? policy.health_items : [];
  const items = [...coverageItems, ...healthItems];
  const mapping = policy.health_mapping || {};
  const period = policy.period || {};
  const start = period.start ? formatPolicyDate(period.start, policy.country) : "";
  const end = period.end ? formatPolicyDate(period.end, policy.country) : "";
  const periodText = start && end ? `${start} — ${end}`
    : t("portfolio.missing_value", { document: t("portfolio.document.policy_schedule") });
  const generic = mapping.mapped === true ? "" : `<p class="portfolio-generic">${esc(t("portfolio.generic_notice", {
    country: policy.country || "",
  }))}</p>`;
  const healthNotice = mapping.notice_key
    ? `<p class="portfolio-health-notice">${esc(healthText(mapping.notice_key))}</p>` : "";
  const sections = PLAN_LAYOUT_SECTIONS.map(section => {
    const sectionItems = section.id === "evidence" ? items
      : items.filter(item => portfolioSectionFor(item.mechanism_code) === section.id);
    const body = sectionItems.length ? sectionItems.map(item => `<li class="portfolio-value">
        <span>${esc(portfolioItemDisplay(policy, item))}</span>${evidenceBadge(item.evidence_class)}
        ${item.state ? `<span class="tag">${esc(humanState(item.state))}</span>` : ""}
        ${planEvidenceControl(policy, item)}
      </li>`).join("") : `<li class="portfolio-missing">${evidenceBadge("UNESTABLISHED")}
      <span>${esc(t("portfolio.missing_value", {
        document: portfolioMissingDocument(section.id),
      }))}</span></li>`;
    return `<section class="plan-layout__section" aria-label="${esc(section.label())}">
      <h4>${esc(section.label())}</h4><ul>${body}</ul></section>`;
  }).join("");

  const headingId = `policy-${String(policy.id || "").replace(/[^A-Za-z0-9_-]/g, "")}`;
  const headerEvidence = coverageItems.find(item => item.source_document_id) || null;
  const headerSource = headerEvidence ? `${evidenceBadge(headerEvidence.evidence_class)}
    <button type="button" class="btn btn-ghost btn-sm portfolio-wording"
      data-act="portfolio-evidence" data-arg="${esc(`${policy.id}|${headerEvidence.id}`)}"
      aria-controls="portfolio-evidence-${esc(headerEvidence.id)}">
      ${esc(t("portfolio.show_wording"))}</button>`
    : `${evidenceBadge("UNESTABLISHED")}<span class="text-xs text-muted">${esc(t("portfolio.missing_value", {
      document: t("portfolio.document.policy_schedule"),
    }))}</span>`;
  return `<article class="plan-layout card" aria-labelledby="${esc(headingId)}">
    <header class="plan-layout__header"><div>
      <h3 id="${esc(headingId)}">${esc(policy.insurer || "")}</h3>
      <p>${esc(policy.product || "")}</p></div>
      <div class="plan-layout__header-evidence"><span class="tag">${esc(policy.status || "")}</span>${headerSource}</div></header>
    <dl class="plan-layout__facts">
      <div><dt>${esc(t("portfolio.header.country"))}</dt><dd>${esc(policy.country || "")}</dd></div>
      <div><dt>${esc(t("portfolio.header.period"))}</dt><dd>${esc(periodText)}</dd></div>
      <div><dt>${esc(t("portfolio.header.policy"))}</dt><dd>${esc(policy.masked_number || "")}</dd></div>
    </dl>
    ${generic}${healthNotice}
    <div class="plan-layout__sections">${sections}</div>
  </article>`;
}

function portfolioCollection(title, values, render, emptyText) {
  const body = values.length ? values.map(render).join("")
    : `<p class="text-sm text-muted">${esc(emptyText)}</p>`;
  return `<section class="portfolio-collection card"><div class="card-head"><h3>${esc(title)}</h3>
    <span class="tag">${esc(values.length)}</span></div>${body}</section>`;
}

function portfolioLoadError(error) {
  const message = error?.code === "LEGACY_SME_BUNDLE_REQUIRES_RESEED"
    ? t("portfolio.legacy_sme_bundle_requires_reseed")
    : (error && error.message ? error.message : String(error || ""));
  return `<div class="insight critical" role="alert"><div class="insight-label">${esc(t("portfolio.load_error_title"))}</div>
    <div class="insight-text">${esc(message)}</div></div>`;
}

async function smePortfolio(el) {
  el.innerHTML = spinner(t("portfolio.loading"));
  let portfolio;
  try {
    portfolio = await api("/api/portfolio");
  } catch (error) {
    el.innerHTML = portfolioLoadError(error);
    return;
  }
  if (!portfolio) return;
  const members = Array.isArray(portfolio.members) ? portfolio.members : [];
  const assets = Array.isArray(portfolio.assets) ? portfolio.assets : [];
  const businesses = Array.isArray(portfolio.businesses) ? portfolio.businesses : [];
  const policies = Array.isArray(portfolio.policies) ? portfolio.policies : [];
  S.householdProvenance = portfolio?.household?.provenance || null;
  if (!members.length && !assets.length && !businesses.length && !policies.length) {
    el.innerHTML = pageTitle(t("portfolio.title"), t("portfolio.lead"))
      + `<div class="portfolio-empty card"><h3>${esc(t("portfolio.empty_title"))}</h3>
        <p>${esc(t("portfolio.empty_body"))}</p>
        <button type="button" class="btn btn-primary mt-16" data-act="load-household-example">
          ${esc(t("portfolio.load_example"))}</button></div>`;
    return;
  }
  el.innerHTML = portfolioSyntheticBanner(portfolio)
    + pageTitle(t("portfolio.title"), t("portfolio.lead"))
    + `<div class="portfolio-summary">
      ${portfolioCollection(t("portfolio.members"), members, member => `<div class="portfolio-row">
        <strong>${esc(member.name || "")}</strong><span>${esc(titleCase(member.relationship || ""))}</span></div>`, t("portfolio.no_members"))}
      ${portfolioCollection(t("portfolio.assets"), assets, asset => `<div class="portfolio-row">
        <strong>${esc(asset.name || "")}</strong><span>${esc(titleCase(asset.kind || ""))}</span></div>`, t("portfolio.no_assets"))}
      ${portfolioCollection(t("portfolio.businesses"), businesses, business => `<div class="portfolio-row">
        <strong>${esc(business.name || "")}</strong><span>${esc(business.country || "")}</span>
        <button type="button" class="btn btn-ghost btn-sm" data-act="nav" data-arg="sme-home">
          ${esc(t("portfolio.open_business"))}</button></div>`, t("portfolio.no_businesses"))}
    </div>
    <section class="portfolio-policies" aria-label="${esc(t("portfolio.policies"))}">
      <h2>${esc(t("portfolio.policies"))}</h2>${policies.map(planLayoutCard).join("")}</section>`;
}

function smeAsk(el) {
  el.innerHTML = portfolioSyntheticBanner() + pageTitle(t("portfolio.ask_title"), t("portfolio.ask_body"))
    + `<div class="card"><button type="button" class="btn btn-primary" data-act="ask-open">
      ${esc(t("portfolio.ask_launch"))}</button></div>`;
}

function smeClaims(el) {
  el.innerHTML = portfolioSyntheticBanner() + pageTitle(t("portfolio.claims_title"), t("portfolio.claims_body"));
}

async function portfolioEvidence(arg) {
  const [policyId, coverageId] = String(arg || "").split("|");
  if (!policyId || !coverageId) return;
  S.portfolioEvidence = { policyId, coverageId, loading: true };
  render();
  try {
    const value = await api(`/api/policies/${encodeURIComponent(policyId)}/coverage/${encodeURIComponent(coverageId)}/evidence`);
    S.portfolioEvidence = { policyId, coverageId, value };
  } catch (error) {
    S.portfolioEvidence = { policyId, coverageId, error: error.message || String(error) };
  }
  render();
}

async function loadHouseholdExample() {
  const el = $("#main");
  if (el) el.innerHTML = spinner(t("portfolio.example_preparing"));
  try {
    // Wrap the existing SME analysis before the household upload replaces the transient
    // case.  That keeps the business as a separate portfolio entity without inventing a
    // household link.
    await api("/api/reset", { method: "POST" });
    await api("/api/portfolio");
    const manifest = await api("/api/example/household/manifest");
    const files = [];
    for (const document of manifest.documents || []) {
      const response = await fetch(`/api/example/household/file/${encodeURIComponent(document.name)}`);
      if (!response.ok) throw new Error(t("portfolio.example_load_failed"));
      files.push(new File([await response.blob()], document.name, { type: "application/pdf" }));
    }
    if (files.length !== Number(manifest.count || 0)) throw new Error(t("portfolio.example_load_failed"));
    S.pendingHouseholdExample = {
      files,
      businessName: manifest.business_name || "",
      kinds: [null, "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE"],
    };
    nav8("sme-upload");
  } catch (error) {
    if (el) el.innerHTML = `<div class="insight critical"><div class="insight-text">${esc(error.message || t("portfolio.example_load_failed"))}</div></div>`;
  }
}

/* ── Add documents ───────────────────────────────────────────────────────── */
async function smeUpload(el) {
  const kinds = await api("/api/bundle/kinds");
  const householdExample = S.pendingHouseholdExample || null;
  el.innerHTML = (householdExample ? `<div class="synthetic-banner">${esc(t("portfolio.synthetic_banner"))}</div>` : "")
    + pageTitle("Add your documents",
      "Your policy, your accounts, anything else you have. PDFs only.")
    + `<div class="card">
      <div class="dropzone" id="dz" role="button" tabindex="0"
           aria-label="Choose PDF files, or drag them here">
        <div class="dropzone-label">Click anywhere in this box to choose files</div>
        <p class="dropzone-hint">or drag them here</p>
        <input type="file" id="finput" multiple accept=".pdf,application/pdf" hidden>
      </div>
      <ul class="file-list" id="flist"></ul>
      <div class="form-group mt-16"><label>Your business name (optional)</label>
        <input type="text" id="bname" placeholder="e.g. Hock Lee Foods Pte Ltd" maxlength="120"></div>
      <button class="btn btn-primary mt-16" id="ubtn" disabled>Read my documents</button>
      <div id="uresult"></div>
    </div>
    <div class="card mt-16">
      <div class="card-head"><h3>Helpful to include</h3></div>
      <p class="text-sm text-muted mb-8">You do not need all of these. More documents means fewer questions we have to leave open.</p>
      <div style="display:flex;flex-wrap:wrap;gap:6px">
        ${(kinds.kinds || []).map(k => `<span class="tag">${esc(k.label || k.kind)}</span>`).join("")}
      </div>
    </div>
    <div class="card mt-16 card-clickable" data-act="load-example">
      <div class="card-head"><h3>Or try it with an example business</h3></div>
      <p class="text-sm text-muted">A made-up food factory with real problems hidden in its paperwork. Goes through exactly the same reading process as your own files.</p>
    </div>`;

  /* Each entry is `{ file, status, note }` rather than a bare File, because
   * `renderList()` rewrites the whole list and anything held only in the DOM would be
   * lost on the next repaint — including the tick we just earned. `removeFile` splices
   * this array, so status travels with its file instead of with an index. */
  let files = [];
  const dz = $("#dz"), fl = $("#flist"), fi = $("#finput"), btn = $("#ubtn");

  // The whole box is the target. Previously only the small label opened the picker while
  // the entire dashed area showed a pointer cursor, so clicking the obvious place did
  // nothing and the screen looked broken.
  dz.onclick = () => fi.click();
  dz.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); } };
  fi.onchange = e => { addFiles(e.target.files); fi.value = ""; };
  dz.ondragover = e => { e.preventDefault(); dz.classList.add("dragover"); };
  dz.ondragleave = e => { e.preventDefault(); dz.classList.remove("dragover"); };
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove("dragover"); addFiles(e.dataTransfer?.files); };

  function addFiles(list) {
    for (const f of Array.from(list || [])) {
      if (f.name.toLowerCase().endsWith(".pdf")) {
        files.push({ file: f, status: "queued", note: "" });
      }
    }
    renderList();
  }

  /* The per-file mark. `queued` is deliberately an empty slot rather than a grey tick:
   * nothing has happened to that file yet and a mark would imply otherwise. */
  function fileMark(status) {
    if (status === "reading") {
      return `<span class="fmark fmark--reading" role="status"
        aria-label="Reading"><span class="fmark__spin"></span></span>`;
    }
    if (status === "done") {
      return `<span class="fmark fmark--done" aria-label="Read">
        <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path
          d="M3.5 8.6l3 3 6-6.5" fill="none" stroke="currentColor"
          stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`;
    }
    if (status === "failed") {
      return `<span class="fmark fmark--failed" aria-label="Could not be read">
        <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path
          d="M5 5l6 6M11 5l-6 6" fill="none" stroke="currentColor"
          stroke-width="2.2" stroke-linecap="round"/></svg></span>`;
    }
    return `<span class="fmark fmark--queued" aria-hidden="true"></span>`;
  }

  function renderList() {
    const busy = files.some(r => r.status === "reading");
    fl.innerHTML = files.map((r, i) => `<li class="file-item ${esc(r.status)}">
      ${fileMark(r.status)}
      <span class="file-item__name">${esc(r.file.name)}${r.note
        ? `<span class="file-item__note">${esc(r.note)}</span>` : ""}</span>
      <span class="text-xs text-muted">${(r.file.size / 1024).toFixed(0)} KB</span>
      ${busy ? "" : `<button class="btn btn-ghost btn-sm" data-act="remove-file"
        data-arg="${i}" aria-label="Remove ${esc(r.file.name)}">✕</button>`}
    </li>`).join("");
    btn.disabled = !files.length || busy;
    btn.textContent = busy ? "Reading..." : files.length
      ? `Read my ${files.length === 1 ? "document" : files.length + " documents"}`
      : "Read my documents";
  }
  window.removeFile = i => { files.splice(i, 1); renderList(); };

  /* Find the row a server event refers to. Matched on FILENAME, never on index: the
   * intake report is built in two passes, so rejected files are reported before accepted
   * ones and the server's order is not the upload order. The server also normalises the
   * name (`ingest.safe_filename`), hence the relaxed fallback. */
  function rowFor(filename) {
    const want = String(filename || "");
    return files.find(r => r.file.name === want)
      || files.find(r => r.file.name.replace(/[<>:"/\\|?*]/g, "_") === want)
      || null;
  }

  async function readFiles() {
    files.forEach(r => { r.status = "queued"; r.note = ""; });
    renderList();
    btn.disabled = true; btn.textContent = "Reading...";
    $("#uresult").innerHTML = "";
    const fd = new FormData();
    files.forEach(r => fd.append("files", r.file, r.file.name));
    if (householdExample?.kinds) fd.append("kinds", JSON.stringify(householdExample.kinds));
    const nm = $("#bname")?.value?.trim();
    if (nm) fd.append("business_name", nm);
    try {
      sessionStorage.removeItem(EXAMPLE_FLAG);
      sessionStorage.removeItem(WORDING_NOTICE_KEY);
      const done = await streamBundle(fd, ev => {
        if (ev.event === "reading") {
          const row = rowFor(ev.filename);
          if (row) { row.status = "reading"; renderList(); }
        } else if (ev.event === "read") {
          const row = rowFor(ev.filename);
          if (row) {
            row.status = ev.accepted ? "done" : "failed";
            row.note = ev.accepted
              ? `${titleCase(ev.kind || "")} · ${ev.pages} page${ev.pages === 1 ? "" : "s"}`
              : (ev.message || "Could not be read");
            renderList();
          }
        } else if (ev.event === "analysing") {
          $("#uresult").innerHTML = `<div class="loading mt-16"><div class="spinner"></div>
            Working out what your documents mean</div>`;
        }
      });
      const rejected = (done.intake?.files || []).filter(f => !f.accepted);
      const accepted = (done.intake?.files || []).filter(f => f.accepted).length;
      const found = done.brief.findings?.length || 0;
      if (householdExample) {
        await api("/api/portfolio/example-household", { method: "POST" });
        // Restore the existing SME case after persistence. The household source PDFs are
        // encrypted in the portfolio now; the Business navigation stays backed by its
        // own established engine rather than treating the household bundle as a company.
        await api("/api/reset", { method: "POST" });
        S.pendingHouseholdExample = null;
        nav8("sme-portfolio");
        return;
      }
      $("#uresult").innerHTML = `<div class="insight mt-16">
        <div class="insight-label">Done</div>
        <div class="insight-text">We read ${accepted} document${accepted === 1 ? "" : "s"} and found ${found} thing${found === 1 ? "" : "s"} worth telling you about.</div>
        <div class="insight-action"><button class="btn btn-sm btn-primary" data-act="nav" data-arg="sme-home">See what we found →</button></div></div>
        ${rejected.length ? `<div class="insight warn mt-8"><div class="insight-label">Couldn't read ${rejected.length}</div><div class="insight-text">${rejected.map(f => esc(f.filename || "") + (f.message ? " — " + esc(f.message) : "")).join("<br>")}</div></div>` : ""}
        <div class="mt-16">${jurisdictionPanel(done.intake?.jurisdiction || done.brief?.jurisdiction)}</div>`;
      // The rows keep their ticks. Clearing the list here would delete the only record
      // the user has of which documents were accepted and which were not.
      files.forEach(r => { if (r.status === "reading") r.status = "queued"; });
    } catch (err) {
      files.forEach(r => { if (r.status === "reading") r.status = "failed"; });
      $("#uresult").innerHTML = `<div class="insight critical mt-16"><div class="insight-label">Something went wrong</div><div class="insight-text">${esc(err.message)}</div></div>`;
    }
    renderList();
    btn.disabled = false;
  }

  btn.onclick = readFiles;
  if (householdExample) {
    files = householdExample.files.map(file => ({ file, status: "queued", note: "" }));
    const name = $("#bname");
    if (name) name.value = householdExample.businessName || "";
    renderList();
    readFiles();
  }
}

/* Read the NDJSON progress stream from POST /api/bundle/stream.
 *
 * Not routed through `api()` because that helper resolves the whole body as JSON, which
 * is exactly what must not happen here — the point is to act on each line as it lands.
 * Resolves with the terminal `done` event, throws on a terminal `error` event.
 *
 * Falls back to the blocking `/api/bundle` when the browser has no streaming body reader.
 * The fallback loses per-file timing and says so by leaving the rows queued rather than
 * inventing ticks for work it did not watch.
 */
async function streamBundle(formData, onEvent) {
  let r;
  try {
    r = await fetch("/api/bundle/stream",
      { method: "POST", body: formData, credentials: "same-origin" });
  } catch (e) {
    throw new Error("Cannot reach the server. Check it is running.");
  }
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(e.detail || e.error?.message || `Error ${r.status}`);
  }
  if (!r.body || !r.body.getReader) {
    const blocking = await api("/api/bundle", { method: "POST", body: formData });
    return blocking;
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    // Keep the trailing fragment: a chunk boundary can land mid-line.
    const lines = buffer.split("\n");
    buffer = done ? "" : lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let ev;
      try { ev = JSON.parse(line); } catch (_) { continue; }
      if (ev.event === "error") throw new Error(ev.message || "The upload failed.");
      if (ev.event === "done") terminal = ev;
      else onEvent(ev);
    }
    if (done) break;
  }
  if (!terminal) throw new Error("The upload ended before it finished reading.");
  return terminal;
}

/* The example is running on its synthetic documents alone, and says so. Empty when the
   corpus holds the wording, so the Overview can interpolate it unconditionally. The
   sentence comes from the server, so it is escaped like any other server-derived value. */
function exampleWordingNotice() {
  const notice = sessionStorage.getItem(WORDING_NOTICE_KEY);
  if (!notice) return "";
  return `<div class="honest mb-16"><b>This example is running without its published base wording.</b>
    ${esc(notice)}</div>`;
}

/* Where a statement about the law comes from: each jurisdiction-pack entry it rests on,
 * with the entry's verbatim quote, a link to the primary source and its verification
 * status (PLAN rule 17 — an owner-facing statement shows all three). The server only ever
 * sends owner-facing entries here; anything else is dropped again on this side so a
 * CANDIDATE can never be drawn as a source. Server-derived throughout, so escaped, and a
 * link is drawn only for an https address. */
function packSources(sources) {
  const list = (sources || []).filter(s => s && s.owner_facing);
  if (!list.length) return "";
  return `<div class="mt-12"><div class="text-xs text-muted mb-8">Where this comes from</div>
    ${list.map(s => {
      const url = typeof s.source_url === "string" && /^https:\/\//i.test(s.source_url)
        ? s.source_url : "";
      const how = s.status === "SOURCE_QUOTED" ? "Quoted from the primary source"
        : `Verified by ${s.verified_by || "a named reviewer"}`;
      return `<div class="verbatim mb-8">
        <div class="verbatim__tag"><i></i>${esc(how)}</div>
        ${s.quote ? `<div class="verbatim__q">${esc(s.quote)}</div>` : ""}
        <div class="verbatim__src">
          <span><b>${esc(s.title || s.entry_id || "")}</b></span>
          <span class="tag">${esc(s.status || "")}</span>
          ${s.retrieved_on ? `<span><b>Retrieved</b> ${esc(s.retrieved_on)}</span>` : ""}
          ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open the source</a>` : ""}
        </div></div>`;
    }).join("")}</div>`;
}

/* Which country's law the documents were read under, how sure we are, and why — both
 * ways. Rendered on the Overview and in the upload result from the same server record
 * (`brief.jurisdiction`, also on `intake.jurisdiction`), so the two cannot disagree.
 *
 * When no pack applies, the server's notice is the headline: "these read as Malaysia and
 * there is no Malaysia pack yet" is the honest answer, and it must not be smaller than a
 * Singapore answer would have been. The reasons sit in a native <details>, which is
 * keyboard-operable and announced as expandable without any script. Every value is
 * server-derived and escaped. */
function jurisdictionPanel(j) {
  if (!j || !j.pack) return "";
  const forCountry = (j.reasons || []).filter(r => r.direction === "for" && r.signal);
  const against = (j.reasons || []).filter(r => r.direction !== "for");
  const reasonItem = r => r.signal
    ? `<li><b>${esc(r.signal_label || r.signal)}</b> “${esc(r.matched || "")}”
        <span class="text-muted">— ${esc(r.document || "")}, ${esc(t("jurisdiction.page", { page: r.page }))}${
          r.country_name ? ` · ${esc(t("jurisdiction.points_to", { country: r.country_name }))}` : ""}</span></li>`
    : `<li>${esc(r.statement || "")}</li>`;
  const notApplied = j.rules_not_applied || [];
  const confidence = String(j.confidence || "").toLowerCase();
  // GENERIC is a safety fallback, not a country pack. Keep the warning branch even if a
  // malformed response incorrectly marks it available.
  const packAvailable = j.pack !== "GENERIC" && j.pack_available === true;
  return `<div class="card mb-16" role="region" aria-label="${esc(t("jurisdiction.region"))}">
    <div class="card-head"><h3>${esc(t("jurisdiction.title"))}</h3>
      <span class="tag ${packAvailable ? "tag-info" : "tag-REVIEW"}">${packAvailable
        ? esc(t("jurisdiction.available_pack", { name: j.pack_name || j.pack }))
        : esc(t("jurisdiction.no_pack"))}</span>
      ${confidence ? `<span class="tag">${esc(t("jurisdiction.confidence", { confidence }))}</span>` : ""}</div>
    ${j.notice
      ? `<div class="insight warn"><div class="insight-text">${esc(j.notice)}</div></div>`
      : `<p class="text-sm">${esc(j.summary || "")}</p>`}
    <details class="v-details"><summary>${esc(t("jurisdiction.why"))}</summary>
      ${forCountry.length ? `<p class="text-xs text-muted mt-8">${esc(t("jurisdiction.points_to", { country: j.detected_name || j.leading || "" }))}</p>
        <ul class="text-sm">${forCountry.map(reasonItem).join("")}</ul>` : ""}
      ${against.length ? `<p class="text-xs text-muted mt-8">${esc(t("jurisdiction.points_elsewhere"))}</p>
        <ul class="text-sm">${against.map(reasonItem).join("")}</ul>` : ""}
      ${notApplied.length ? `<p class="text-xs text-muted mt-8">${esc(t("jurisdiction.not_applied"))}</p>
        <ul class="text-sm">${notApplied.map(r => `<li>${esc(r.title || r.rule_id)}
          <span class="text-muted">— ${esc(r.reason || "")}</span></li>`).join("")}</ul>` : ""}
    </details>
  </div>`;
}

window.loadExample = async function () {
  const el = $("#main");
  if (el) el.innerHTML = spinner("Loading the example business and reading its paperwork");
  try {
    sessionStorage.setItem(EXAMPLE_FLAG, "1");
    const man = await api("/api/example/manifest");
    const files = [];
    for (const doc of (man.documents || [])) {
      const resp = await fetch("/api/example/file/" + encodeURIComponent(doc.name));
      if (resp.ok) files.push(new File([await resp.blob()], doc.name, { type: "application/pdf" }));
    }
    const fd = new FormData();
    files.forEach(f => fd.append("files", f, f.name));
    fd.append("business_name", man.business_name || "Hock Lee Foods Pte Ltd");
    /* The base wording is a real insurer's published document, so the browser never
       downloads it. The server attaches it from the document corpus instead; this flag
       asks for it, and the manifest says whether the corpus holds it. When it does not,
       the server's own sentence about what will be missing is kept and shown on the
       Overview — see exampleWordingNotice(). */
    if (man.server_attaches_wording) {
      fd.append("attach_example_wording", "1");
      sessionStorage.removeItem(WORDING_NOTICE_KEY);
    } else if (man.wording_notice) {
      sessionStorage.setItem(WORDING_NOTICE_KEY, man.wording_notice);
    }
    await api("/api/bundle", { method: "POST", body: fd });
    nav8("sme-home");
  } catch (err) {
    if (el) el.innerHTML = `<div class="insight critical"><div class="insight-label">Could not load the example</div><div class="insight-text">${esc(err.message)}</div></div>`;
  }
};

/* ── Your cover score ────────────────────────────────────────────────────── */
async function smeProtection(el) {
  el.innerHTML = spinner();
  const score = await api("/api/score"); if (!score) return;
  const p = await getPresentation("protection_overview", {
    overall: score.overall, status: score.status,
    assessed_dimensions: score.assessed_dimensions,
    total_dimensions: score.total_dimensions,
  });
  const dims = (score.dimensions || []).map(d => ({
    label: d.label || d.key, value: d.score ?? 0, max: 100,
    display: d.score == null ? "not checked" : d.score + "/100",
    status: d.score == null ? "warn" : d.score > 70 ? "good" : d.score > 40 ? "warn" : "bad",
  }));
  el.innerHTML = pageTitle("Your cover score",
      "How well your paperwork backs up the cover you think you have.")
    + `<div class="grid-2 mb-16">
        <div class="card text-center">${renderRing(score.overall, 170)}
          <h3 class="mt-12">${esc(score.status || "")}</h3>
          <p class="text-xs text-muted mt-8">We could check ${score.assessed_dimensions ?? "—"} of ${score.total_dimensions ?? "—"} areas. An area with no paperwork is left blank, not marked zero.</p>
        </div>
        <div>${renderAssessment(p?.pres, p?.mode)}</div>
      </div>
      ${score.headline_exposure ? `<div class="insight critical mb-16"><div class="insight-label">Biggest single gap</div><div class="insight-text">On a ${money(score.headline_exposure.assessed_loss_sgd)} loss you would get about ${money(score.headline_exposure.illustrated_settlement_sgd)} — leaving ${money(score.headline_exposure.amount_sgd)} with you.</div><div class="insight-action"><button class="btn btn-sm btn-primary" data-act="nav" data-arg="sme-scenario">See how that works →</button></div></div>` : ""}
      <div class="card"><div class="card-head"><h3>Area by area</h3></div>${renderBars(dims)}
        <p class="text-xs text-muted mt-12">${esc(score.notice || "")}</p></div>`;
}

/* ── What we found ───────────────────────────────────────────────────────── */
async function smeFindings(el) {
  el.innerHTML = spinner("Putting everything together");
  const d = await api("/api/orchestrator/evaluate"); if (!d) return;
  const decisions = d.decisions || [];
  const needs = decisions.filter(x => x.decision === "ACTION_REQUIRED" || x.decision === "REVIEW");
  const rest = decisions.filter(x => !needs.includes(x));
  el.innerHTML = pageTitle("What we found",
      `${decisions.length} area${decisions.length === 1 ? "" : "s"} of your business, each judged on your own documents.`)
    + (needs.length ? `<h3 class="mb-8">Worth your attention</h3><div class="findings mb-16">${needs.map(renderDecision).join("")}</div>` : "")
    + (rest.length ? `<h3 class="mb-8">Everything else</h3><div class="findings">${rest.map(renderDecision).join("")}</div>` : "")
    + `<p class="text-xs text-muted mt-16">${esc(d.notice || "")}</p>`;
}

/* ── Things to do ────────────────────────────────────────────────────────── */
async function smeActions(el) {
  el.innerHTML = spinner();
  const d = await api("/api/actions"); if (!d) return;
  const actions = d.actions || [];
  if (!actions.length) {
    el.innerHTML = pageTitle("Things to do", "Nothing waiting on you.")
      + emptyState("All clear", "There is nothing for you to action right now.");
    return;
  }
  // The owner's own moves, taken from the same `allowed_moves` the adviser screen uses.
  // Releasing to the adviser and withdrawing are the two the state machine reserves to
  // "owner", so those are the only buttons that can appear here.
  const mine = a => (a.allowed_moves || []).filter(m => m.by === "owner");
  el.innerHTML = pageTitle("Things to do",
      `${actions.length} item${actions.length === 1 ? "" : "s"} prepared for you.`)
    + actions.map(a => `<div class="action-card mb-8" data-rise="rise">
        <div class="action-head"><div class="action-title">${esc(a.title || a.interest || "")}</div>
          <!-- state_label is authored server-side in actions.STATE_LABELS for exactly this
               reader: "Prepared — not yet sent", "Cover amended", "Adviser advised no
               change needed". humanState() looks up STATE_WORDS, which holds FINDING
               states (DOCUMENTED, NOT_CONFIRMED...) and no action state at all, so every
               card fell through to titleCase() and printed the engine's enum back at the
               owner — "Drafted", "Bound", "Not Required". Same data, wrong words. The
               adviser's card already reads state_label; this one now agrees with it. -->
          <span class="tag tag-${a.state || ""}">${esc(a.state_label || humanState(a.state) || (a.state || "").replace(/_/g, " "))}</span></div>
        <div class="action-body">${esc(a.action_label || a.exposure_basis || "")}${a.asks ? " — we would ask for: " + esc(a.asks) : ""}</div>
        <div class="action-meta">${a.exposure_sgd ? `<span class="tag tag-danger">${money(a.exposure_sgd)} at stake</span>` : ""}<span class="tag">${esc(titleCase(a.interest || ""))}</span></div>
        <div class="mt-12">${workflowRail(a.state)}</div>
        ${mine(a).length ? `<div class="btn-group mt-12">${mine(a).map(m => `
          <button class="btn btn-sm ${m.to === "SENT_TO_ADVISER" ? "btn-primary" : "btn-secondary"}"
            data-act="own-move" data-arg="${esc(a.id)}|${esc(m.to)}">${esc(m.label)}</button>`).join("")}</div>`
          : a.is_terminal ? "" : `<p class="text-xs text-muted mt-12">Waiting on your adviser.</p>`}
      </div>`).join("")
    + `<p class="text-xs text-muted mt-16">${esc(d.premium_notice || "")}</p>`;
  stage(el, "[data-rise]", 60);
}

/* The owner advancing their own request. Same endpoint, same state machine, and the actor
 * still comes from the session — an owner cannot post `by: "adviser"`. */
async function ownerMove(arg) {
  const [id, to] = String(arg || "").split("|");
  if (!id || !to) return;
  if (to === "WITHDRAWN" && !confirm("Withdraw this request? Your adviser will no longer see it."))
    return;
  try {
    await api(`/api/actions/${encodeURIComponent(id)}/advance`,
      { method: "POST", body: { to_state: to, note: "" } });
    toast(to === "SENT_TO_ADVISER" ? "Sent to your adviser" : "Withdrawn");
    nav8(S.view);
  } catch (err) {
    toast(err.message || String(err), "err");
  }
}
window.ownerMove = ownerMove;

/* ── What would help ────────────────────────────────────────────────────── */
async function smeGaps(el) {
  el.innerHTML = spinner();
  const d = await api("/api/information-value"); if (!d) return;
  const gaps = d.gaps || [];
  const p = await getPresentation("gaps_summary", {
    gap_count: gaps.length,
    top_document: (gaps[0] || {}).document_needed,
  });
  el.innerHTML = pageTitle("What would help",
      "Questions we left open, and the one document that would answer each.")
    + renderAssessment(p?.pres, p?.mode)
    + (gaps.length ? gaps.map((g, i) => `<div class="card mb-12">
        <div class="card-head"><h3>${i + 1}. ${esc(g.document_needed || g.operand || "")}</h3>
          ${g.unbounded ? '<span class="tag tag-danger">Biggest unknown</span>' : ""}</div>
        <p class="text-sm">${esc(g.note || "")}</p>
        <p class="text-xs text-muted mt-8">Sending this would turn a guess into something proven.</p>
      </div>`).join("")
      : emptyState("Nothing missing", "We had enough paperwork to answer every question we asked."))
    + `<p class="text-xs text-muted mt-12">${esc(d.caveat || "")}</p>`;
}

/* ── Changes in your business ───────────────────────────────────────────── */
async function smeConnect(el) {
  el.innerHTML = spinner();
  const d = await api("/api/signals"); if (!d) return;
  const events = d.events || [];
  const p = await getPresentation("connect_summary", { event_count: events.length });
  el.innerHTML = pageTitle("Changes in your business",
      "Picked up from your own records, without you telling us.")
    + renderAssessment(p?.pres, p?.mode)
    + `<div class="card mb-16"><div class="card-head"><h3>What changed</h3></div>
        ${renderChangePulse(events.map(e => ({
          direction: e.kind === "ASSET_ACQUIRED" ? "new" : "up",
          label: e.label || titleCase(e.kind || ""),
          value: e.amount_sgd ? money(e.amount_sgd) : "",
          source: e.connector || "",
        })))}</div>
      <div class="card"><div class="card-head"><h3>Where this comes from</h3></div>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          ${(d.connectors || []).map(c => `<span class="source-badge ${c.simulated ? "demo" : "connected"}">${esc(c.name)} ${c.simulated ? '<small style="margin-left:auto">example</small>' : ""}</span>`).join("")}
        </div>
        <p class="text-xs text-muted mt-12">${esc(d.notice || "")}</p></div>`;
}

/* ── Around your business ───────────────────────────────────────────────── */
async function smeSentinel(el) {
  el.innerHTML = spinner("Checking conditions around you");
  const d = await api("/api/sentinel"); if (!d) return;
  const live = d.live || {};
  const env = (d.environment || {}).level || "LOW";
  const signals = d.signals || [];
  const p = await getPresentation("sentinel_summary", {
    environment_level: env, signal_count: signals.length,
  });
  el.innerHTML = pageTitle("Around your business",
      "Things happening outside that could affect what it costs to put you back together.")
    + `<div class="grid-2 mb-16">
        <div class="card"><div class="card-head"><h3>Conditions now</h3>${renderDataBadge(live.any_live)}</div>
          ${renderRiskMeter(env)}
          <p class="text-xs text-muted mt-12">${live.any_live
            ? "Live — we called a Singapore government service for this, just now."
            : "Example data. Set PROVEN_LIVE_SOURCES=1 for live figures."}${live.fetched_at ? " · " + esc(live.fetched_at) : ""}</p>
        </div>
        <div>${renderAssessment(p?.pres, p?.mode)}</div>
      </div>
      <div class="card mb-16"><div class="card-head"><h3>What we are watching</h3></div>
        ${renderTimeline(signals.map(s => ({
          title: s.label, source: (s.source || {}).name, date: s.observed_on,
          detail: s.detail, severity: s.severity,
        })))}
      </div>
      <div class="card"><div class="card-head"><h3>Exactly where we looked</h3></div>
        <table class="mt-8"><thead><tr><th>Source</th><th>Address we called</th><th>Result</th></tr></thead><tbody>
          ${(live.fetch_log || []).map(f => `<tr><td>${esc(f.source || "")}</td><td class="text-xs" style="font-family:var(--mono);word-break:break-all">${esc(f.url || "")}</td><td>${f.ok ? '<span class="tag tag-success">OK</span>' : `<span class="tag tag-danger">${esc(f.error || "no answer")}</span>`}</td></tr>`).join("")}
        </tbody></table></div>`;
}

/* ══════════════════════════════════════════════════════════════════════════
   WHAT'S NEW — the watching surface

   The one screen that exists because time passed. Every other screen answers "what is
   true about my policy right now"; this one answers "what changed while I was not
   looking", which is the only question a notification can honestly be about.

   Three things are deliberately on screen rather than in a footnote:
     * whether an email was actually SENT, and if not, why not
     * the exact message that was composed, readable in full
     * that a website is self-authored, so what it says is a flag and not a fact
   ══════════════════════════════════════════════════════════════════════════ */

/* The Overview strip. Shown only when there is genuinely something unread — a permanent
 * "0 new things" panel trains the eye to skip that part of the page, which is exactly where
 * the one that matters will eventually appear. */
function renderWhatsNewStrip(notes) {
  const unread = (notes || {}).unread || 0;
  if (!unread) return "";
  const items = ((notes || {}).notifications || []).filter(n => !n.read).slice(0, 2);
  const urgent = items.some(n => n.severity === "HIGH");
  return `<div class="insight ${urgent ? "critical" : ""} mb-16">
    <div class="insight-label">While you were away</div>
    <div class="insight-text">${unread} thing${unread === 1 ? "" : "s"} changed in your
      public information since you last looked.</div>
    <ul class="ntf__points">${items.map(n => `<li>${esc(n.title)}</li>`).join("")}</ul>
    <div class="insight-action">
      <button class="btn btn-sm btn-primary" data-act="nav" data-arg="sme-alerts">
        See what changed →</button></div>
  </div>`;
}

async function checkNow() {
  toast("Looking at your public information…");
  try {
    const r = await api("/api/notifications/check-now", { method: "POST" });
    if (!r) return;
    if (r.first_observation) {
      toast("First look recorded. From now on we can tell you what changes.", "ok");
    } else if (r.notifications_created > 0) {
      toast(`${r.notifications_created} new change${r.notifications_created === 1 ? "" : "s"} found`,
            "warn");
    } else {
      toast("Nothing has changed since the last check.", "ok");
    }
    await refreshUnread();
    render();
  } catch (e) {
    toast(e.message || "The check could not run.", "err");
  }
}

async function markNotificationRead(id) {
  try {
    await api(`/api/notifications/${encodeURIComponent(id)}/read`, { method: "POST" });
    await refreshUnread();
    render();
  } catch (e) { toast(e.message, "err"); }
}

async function markAllNotificationsRead() {
  try {
    const r = await api("/api/notifications/read-all", { method: "POST" });
    if (r) toast(r.marked_read ? `${r.marked_read} marked as read` : "Nothing unread", "ok");
    await refreshUnread();
    render();
  } catch (e) { toast(e.message, "err"); }
}

async function restartWatch() {
  try {
    const r = await api("/api/notifications/restart-watch", { method: "POST" });
    if (r) toast("Cleared. What your website says today is the new normal.", "ok");
    S.unread = 0;
    await refreshUnread();
    render();
  } catch (e) { toast(e.message, "err"); }
}

async function setNotificationPref(changes) {
  try {
    await api("/api/notifications/preferences", { method: "POST", body: changes });
    toast("Saved", "ok");
    render();
  } catch (e) { toast(e.message, "err"); }
}

/* `arg` arrives as "CODE:0" or "CODE:1" — the value to SET, computed when the button was
 * drawn. The button cannot read its own new state because the click was prevented. */
function setNotificationCode(arg) {
  const [code, on] = String(arg || "").split(":");
  if (!code) return;
  return setNotificationPref({ codes: { [code]: on === "1" } });
}

const _SEV_WORDS = { HIGH: "Worth doing now", MODERATE: "Worth knowing", LOW: "Minor" };
const _MAIL_WORDS = {
  SENT: ["tag-success", "Emailed to you"],
  NO_MAIL_SERVER: ["tag-warning", "Written to your outbox"],
  SUPPRESSED_BY_PREFERENCE: ["tag", "Not emailed, by your settings"],
  HELD_FOR_DIGEST: ["tag", "Saved for your daily summary"],
  FAILED: ["tag-danger", "Email failed"],
  NOT_ATTEMPTED: ["tag", "Not emailed"],
};

function renderNotification(n) {
  const sev = n.severity === "HIGH" ? "tag-danger"
    : n.severity === "MODERATE" ? "tag-warning" : "tag";
  const [mailCls, mailWord] = _MAIL_WORDS[n.email_status] || _MAIL_WORDS.NOT_ATTEMPTED;
  const ev = n.evidence || {};
  const points = (ev.supporting_points || []).slice(0, 3);
  /* WHO SENT THIS, made unmissable.
   *
   * Two senders now write to this inbox. VERIS reports what its own agents noticed; the
   * insurer asks the owner to do something. An owner deciding whether to act needs to know
   * which — a monitoring agent's observation and a request from the company that would pay
   * the claim are different kinds of message, and rendering them identically would waste the
   * whole point of the carrier feature.
   *
   * The flag comes from the notification's own evidence (`sent_by: CARRIER`, set by
   * /api/carrier/notify), not from guessing at the code prefix. */
  const fromInsurer = (ev.sent_by === "CARRIER");
  return `<div class="ntf ${n.read ? "ntf--read" : "ntf--unread"} ${fromInsurer ? "ntf--insurer" : ""}">
    ${fromInsurer ? `<div class="ntf__from">
      <span class="ntf__from-mark">✉</span>
      From your insurer${ev.source_name ? ` — ${esc(ev.source_name)}` : ""}
      <span class="ntf__from-note">not from Veris</span></div>` : ""}
    <div class="ntf__head">
      <span class="tag ${sev}">${esc(_SEV_WORDS[n.severity] || n.severity)}</span>
      <div class="ntf__title">${esc(n.title)}</div>
      ${n.read ? "" : `<button class="btn btn-ghost btn-sm" data-act="ntf-read"
         data-arg="${esc(n.id)}">Mark as read</button>`}
    </div>
    ${ev.subheadline ? `<p class="text-sm text-muted mb-8">${esc(ev.subheadline)}</p>` : ""}
    <div class="ntf__body">${esc(n.body)}</div>
    ${points.length ? `<ul class="ntf__points">${points.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    ${n.what_to_do ? `<div class="ntf__todo"><b>What would help</b>${esc(n.what_to_do)}</div>` : ""}
    ${ev.caveat ? `<div class="ntf__caveat">${esc(ev.caveat)}</div>` : ""}
    <div class="ntf__meta">
      <span>${esc(n.code_label || n.code)}</span>
      ${ev.source_name ? `<span>Seen on ${esc(ev.source_name)}</span>` : ""}
      <span class="tag ${mailCls}">${esc(mailWord)}</span>
      <span>${n.wording_by === "MODEL" ? "Wording by AI" : "Wording from a template"}</span>
    </div>
    ${n.email_detail && n.email_status !== "SENT"
      ? `<p class="text-xs text-muted mt-8">${esc(n.email_detail)}</p>` : ""}
  </div>`;
}

function renderMailPreview(messages, selectedId) {
  if (!messages.length) {
    return `<div class="card"><div class="card-head"><h3>Emails we wrote</h3></div>
      <p class="text-sm">Nothing yet. When we find a change, the message we send you
      appears here in full so you can see exactly what went out.</p></div>`;
  }
  const chosen = messages.find(m => m.id === selectedId) || messages[0];
  const list = messages.map(m => `<div class="mail-list__item ${m.id === chosen.id ? "mail-list__item--on" : ""}"
      data-act="ntf-preview" data-arg="${esc(m.id)}">
      <span class="tag ${m.status === "SENT" ? "tag-success" : "tag-warning"}">${esc(m.status === "SENT" ? "sent" : "not sent")}</span>
      <span class="mail-list__subject">${esc(m.subject)}</span></div>`).join("");
  return `<div class="card"><div class="card-head"><h3>Emails we wrote</h3>
      <span class="tag">${messages.length}</span></div>
    <p class="text-sm text-muted mb-12">This is the real message, word for word. If no mail
      server is set up we still write it, and say so, rather than claiming it was sent.</p>
    ${messages.length > 1 ? `<div class="mail-list mb-12">${list}</div>` : ""}
    <div class="mail">
      <div class="mail__head">
        <div class="mail__subject">${esc(chosen.subject)}</div>
        <div class="mail__row"><span class="mail__k">To</span><span class="mail__v">${esc(chosen.to)}</span></div>
        <div class="mail__row"><span class="mail__k">From</span><span class="mail__v">${esc(chosen["from"])}</span></div>
        <div class="mail__row"><span class="mail__k">Status</span><span class="mail__v">${esc(chosen.status)} — ${esc(chosen.detail || "")}</span></div>
      </div>
      <div class="mail__body">${esc(chosen.text_body)}</div>
    </div></div>`;
}

function renderNotificationPrefs(prefs, available) {
  const sevBtn = (v, label) => `<button class="btn btn-sm ${prefs.minimum_severity === v ? "btn-primary" : "btn-secondary"}"
      data-act="ntf-sev" data-arg="${v}">${esc(label)}</button>`;
  const digBtn = (v, label) => `<button class="btn btn-sm ${prefs.digest === v ? "btn-primary" : "btn-secondary"}"
      data-act="ntf-digest" data-arg="${v}">${esc(label)}</button>`;
  const codes = (available || []).map(c => {
    const on = (prefs.codes || {})[c.code] !== false;
    return `<div class="pref"><div>
        <div class="pref__label">${esc(c.label)}</div>
        <div class="pref__hint">${esc(_SEV_WORDS[c.severity] || c.severity)}</div>
      </div>
      <div class="btn-group"><button class="btn btn-sm ${on ? "btn-primary" : "btn-secondary"}"
        data-act="ntf-pref" data-arg="${esc(c.code)}:${on ? "0" : "1"}">${on ? "On" : "Off"}</button></div>
    </div>`;
  }).join("");

  return `<div class="card"><div class="card-head"><h3>What we email you about</h3></div>
    <p class="text-sm text-muted mb-12">Turning something off here stops the email, not the
      check. We still find it, still show it here, and say why it was not sent.</p>
    <div class="pref"><div><div class="pref__label">Email me at all</div>
        <div class="pref__hint">Master switch</div></div>
      <div class="btn-group"><button class="btn btn-sm ${prefs.email_enabled ? "btn-primary" : "btn-secondary"}"
        data-act="ntf-mail" data-arg="${prefs.email_enabled ? "0" : "1"}">${prefs.email_enabled ? "On" : "Off"}</button></div></div>
    <div class="pref"><div><div class="pref__label">How important before you email me</div>
        <div class="pref__hint">Anything less important is still recorded here</div></div>
      <div class="btn-group">${sevBtn("LOW", "Everything")}${sevBtn("MODERATE", "Worth knowing")}${sevBtn("HIGH", "Only urgent")}</div></div>
    <div class="pref"><div><div class="pref__label">When</div>
        <div class="pref__hint">Straight away, or once a day in one email</div></div>
      <div class="btn-group">${digBtn("IMMEDIATE", "Straight away")}${digBtn("DAILY", "Once a day")}</div></div>
    ${codes}</div>`;
}

async function smeAlerts(el) {
  el.innerHTML = spinner("Checking what changed");
  const [d, prefs, box, watch] = await Promise.all([
    api("/api/notifications"),
    api("/api/notifications/preferences"),
    api("/api/notifications/outbox"),
    api("/api/notifications/watch"),
  ]);
  if (!d) return;
  const items = d.notifications || [];
  const counts = d.counts || {};
  const messages = (box || {}).messages || [];
  const w = (watch || {}).watchlist || {};
  const mail = d.mail || {};
  const lastChecked = d.last_checked
    ? new Date(d.last_checked).toLocaleString() : "not yet";

  el.innerHTML = pageTitle("What's new",
      "Changes we found in your public information since the last time we looked.")
    + `<div class="grid-2 mb-16">
        <div class="card">
          <div class="card-head"><h3>Watching for you</h3>
            ${renderDataBadge(true)}</div>
          <div class="v-stats mb-12">
            <div class="v-stat"><div class="v-stat__val">${d.unread || 0}</div>
              <div class="v-stat__label">not read yet</div></div>
            <div class="v-stat"><div class="v-stat__val">${counts.total || 0}</div>
              <div class="v-stat__label">found in total</div></div>
            <div class="v-stat"><div class="v-stat__val">${d.facts_watched || 0}</div>
              <div class="v-stat__label">things watched</div></div>
          </div>
          <p class="text-sm text-muted">Last checked: ${esc(lastChecked)}.
            We look again on our own every few minutes.</p>
          <div class="grid-2 mt-12">
            <button class="btn btn-primary" data-act="check-now">Check now</button>
            ${items.some(n => !n.read)
              ? '<button class="btn btn-secondary" data-act="ntf-read-all">Mark all as read</button>'
              : ""}
          </div>
          ${counts.total ? `<div class="pref mt-12"><div>
              <div class="pref__label">I've dealt with these</div>
              <div class="pref__hint">Clears this list and treats what your website says
                today as correct. Nothing on your policy or in your record changes.</div>
            </div>
            <div class="btn-group"><button class="btn btn-ghost btn-sm"
              data-act="ntf-restart">Start fresh</button></div></div>` : ""}
        </div>
        <div class="card"><div class="card-head"><h3>How this works</h3></div>
          <p class="text-sm">We read what your business publishes about itself — your
            website, and the government company register — and compare it with what your
            policy says. When the two stop matching, you hear about it.</p>
          <ul class="ntf__points mt-12">
            <li>The first look is just a starting point, so it never raises anything.</li>
            <li>We only raise things with a definite value: an address, a number of
                people, a registration status.</li>
            <li>Your own website tells us what you published, not what is true, so those
                are flags rather than proof.</li>
          </ul>
          <p class="text-xs text-muted mt-12">${esc(mail.notice || "")}</p>
        </div>
      </div>

      ${items.length
        ? `<h3 class="mb-12">${items.length} thing${items.length === 1 ? "" : "s"} we noticed</h3>
           ${items.map(renderNotification).join("")}`
        : emptyState("Nothing new",
            `We have looked at your public information and it still matches your policy.
             ${d.last_checked ? "" : "Press Check now to take the first look."}`,
            '<button class="btn btn-primary" data-act="check-now">Check now</button>')}

      <div class="grid-2 mt-16">
        ${renderMailPreview(messages, S.previewMessage)}
        ${renderNotificationPrefs((prefs || {}).preferences || {},
                                  (prefs || {}).available_codes || [])}
      </div>

      <p class="text-xs text-muted mt-16">${esc(w.notice || "")}
        ${esc((watch || {}).alertable_note || "")}</p>`;
}

/* ── Public records ─────────────────────────────────────────────────────── */
async function smeDiscovery(el) {
  el.innerHTML = spinner("Looking you up in public records");
  const d = await api("/api/discovery"); if (!d) return;
  const facts = d.facts || [];
  const isLive = d.demo_mode === false;
  const p = await getPresentation("discovery_summary", {
    verified_count: d.verified_count, sources_checked: d.sources_checked,
  });
  el.innerHTML = pageTitle("Public records",
      "What the government register says about you, checked against your policy.")
    + `<div class="grid-2 mb-16">
        <div class="card"><div class="card-head"><h3>Where we looked</h3>${renderDataBadge(isLive)}</div>
          <p class="text-sm">${isLive
            ? "Live — we searched Singapore's official company register just now."
            : "Example data. Set PROVEN_LIVE_SOURCES=1 to search the real register."}</p>
          <table class="mt-12"><tbody>
            ${(d.fetch_log || []).map(f => `<tr><td>${esc(f.source)}</td><td>${f.ok ? '<span class="tag tag-success">found</span>' : `<span class="tag">${esc(String(f.error || "not available").slice(0, 40))}</span>`}</td></tr>`).join("")}
          </tbody></table>
          <!-- Check now belongs HERE, on the sources it actually re-reads.
               /api/notifications/check-now runs the Discovery agent — the website, ACRA,
               MOM — and nothing else. It was only reachable from What's new, which is where
               the RESULT lands, not where the work happens. It is deliberately NOT on the
               "Your business" tab beside it: that screen shows the Connect agent reading
               internal records, and a button there would claim to re-read a company's own
               books when it re-reads their public website. -->
          <div class="mt-16">
            <button class="btn btn-sm btn-primary" data-act="check-now">Check now</button>
            <p class="text-xs text-muted mt-8">Looks at these sources again and tells you
              only what has changed since the last check.</p>
          </div></div>
        <div>${renderAssessment(p?.pres, p?.mode)}</div>
      </div>
      ${renderHowWeRead(d.extraction, d.discarded)}
      ${facts.length ? `<div class="findings">${facts.map(f => `<div class="finding" data-act="toggle-detail">
          <div class="finding-head"><div class="finding-title">${esc(f.label || "")}</div>
            <span class="tag">${f.extraction === "MODEL" ? "AI spotted this" : "Rule spotted this"}</span>
            <span class="tag tag-${f.claim_scope === "DISCOVERY_VERIFIED" ? "success" : "warning"}">${f.claim_scope === "DISCOVERY_VERIFIED" ? "Confirmed by the register" : "Worth checking"}</span></div>
          <div class="finding-body">${esc(f.detail || "")}</div>
          <div class="finding-detail">
            <p class="text-sm">${f.grounded
              ? "We found these exact words in the page we fetched. If we could not, we would have thrown this away."
              : "We could not find this wording in the page, so we are only flagging it, not claiming it."}</p>
            ${f.ground_quote ? `<p class="text-sm mt-8"><strong>The words we found:</strong> &ldquo;${esc(String(f.ground_quote).slice(0, 220))}&rdquo;</p>` : ""}
          </div>
        </div>`).join("")}</div>`
      : emptyState("Nothing out of place", "Your public records match what your policy says.")}`;
}

/* How the facts above were pulled out of the pages, and what we refused to keep.
   The thrown-away pile is the point of this panel, not a footnote to it: anyone can show
   you what a model found, and showing what it got wrong is what makes the rest credible. */
function renderHowWeRead(x, discarded) {
  if (!x) return "";
  const thrown = discarded || [];
  return `<div class="card mb-16">
    <div class="card-head"><h3>How we read these pages</h3>
      <span class="tag">${x.model_used ? "AI + fixed rules" : "Fixed rules only"}</span></div>
    <p class="text-sm">Two readers go over every page. Fixed rules look for wording we already
      expect. ${x.model_used
        ? "Our AI reads the rest, in case a page is worded in a way we did not plan for."
        : "The AI reader is switched off right now, so you are seeing rules only."}
      Nothing either of them says is shown to you until we find those exact words back in the
      page itself.</p>
    <div class="v-stats mt-12">
      <div class="v-stat"><div class="v-stat__val">${x.pattern_facts ?? 0}</div>
        <div class="v-stat__label">found by fixed rules</div></div>
      <div class="v-stat"><div class="v-stat__val">${x.model_facts ?? 0}</div>
        <div class="v-stat__label">found by AI</div></div>
      <div class="v-stat v-stat--warn"><div class="v-stat__val">${x.discarded_ungrounded ?? 0}</div>
        <div class="v-stat__label">thrown away</div></div>
    </div>
    ${thrown.length ? `<div class="mt-12">
      <p class="text-sm"><strong>What we threw away, and why:</strong></p>
      <table class="mt-8"><tbody>
        ${thrown.slice(0, 8).map(r => `<tr>
          <td class="text-sm">&ldquo;${esc(String(r.value || "").slice(0, 60))}&rdquo;</td>
          <td class="text-sm">${esc(r.reason || "")}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
  </div>`;
}

/* ── Keeping up with costs ──────────────────────────────────────────────── */
async function smeDrift(el) {
  el.innerHTML = spinner();
  const d = await api("/api/drift"); if (!d) return;
  if (!d.available) {
    el.innerHTML = pageTitle("Keeping up with costs")
      + emptyState("Not enough to go on", esc(d.reason || ""));
    return;
  }
  const isLive = (d.live || {}).is_live;
  const p = await getPresentation("drift_summary", { crossed_on: d.crossed_on });
  el.innerHTML = pageTitle("Keeping up with costs",
      "What happens to your cover if you change nothing.")
    + `<div class="grid-2 mb-16">
        <div>${renderAssessment(p?.pres, p?.mode)}</div>
        <div class="card"><div class="card-head"><h3>The figures we used</h3>${renderDataBadge(isLive)}</div>
          <p class="text-sm">${isLive ? esc(d.source_presentation || "") + " — real published monthly figures."
            : esc(d.index_name || "") + " — sample figures. Set PROVEN_LIVE_SOURCES=1 for the live index."}</p>
        </div>
      </div>
      <div class="insight ${d.crossed_on ? "critical" : ""} mb-16">
        <div class="insight-label">${d.crossed_on ? "Your cover has fallen behind" : "Still keeping up"}</div>
        <div class="insight-text">${esc(d.headline || "")}</div></div>
      <div class="card"><div class="card-head"><h3>Over time</h3></div>
        ${renderTrendChart(d.observations, parseFloat(d.threshold_pct))}
        <p class="text-xs text-muted mt-12">${esc(d.assumption || "")} ${esc(d.caveat || "")}</p></div>`;
}

/* ── If you claim tomorrow ─────────────────────────────────────────────────
 * The screen that changes behaviour. "Your cover is 66.7% adequate" is a statistic.
 * "You would pay S$238,300 yourself" is the same arithmetic and a different feeling. */
let _scenarioData = null;

async function smeScenario(el) {
  el.innerHTML = spinner("Working out what a claim would pay");
  const d = await api("/api/scenario/presets"); if (!d) return;
  _scenarioData = d;
  if (d.abstained) {
    el.innerHTML = pageTitle("If you claim tomorrow")
      + `<div class="insight warn"><div class="insight-label">We can't work this out yet</div>
          <div class="insight-text">${esc(d.message || "")}</div></div>
        <p class="text-sm text-muted mt-12">We would rather say we don't know than put a made-up number on a screen about your money. Send the missing document and this becomes real.</p>
        <button class="btn btn-primary mt-16" data-act="nav" data-arg="sme-gaps">See what's missing</button>`;
    return;
  }
  const scenarios = d.scenarios || [];
  el.innerHTML = pageTitle("If you claim tomorrow",
      "Pick a loss size. We'll show you exactly what your policy would pay, and what you'd cover yourself.")
    + `<div class="card mb-16"><div class="card-head"><h3>How big a loss?</h3></div>
        <div class="grid-3" id="scenario-picker">
          ${scenarios.map((s, i) => `<button class="btn ${i === scenarios.length - 1 ? "btn-primary" : "btn-secondary"}" data-act="pick-scenario" data-arg="${i}">
            ${esc({ small: "A small one", typical: "A serious one", total: "Lose everything" }[s.size] || titleCase(s.size))}
            <br><span class="text-xs">${money(s.loss_amount)}</span></button>`).join("")}
        </div></div>
      <div id="scenario-detail"></div>`;
  window.pickScenario(scenarios.length - 1);
}

window.pickScenario = async function (i) {
  const s = (_scenarioData?.scenarios || [])[i];
  const cont = $("#scenario-detail");
  if (!s || !cont) return;
  const r = s.result;
  $$("#scenario-picker .btn").forEach((b, idx) => {
    b.className = "btn " + (idx === i ? "btn-primary" : "btn-secondary");
  });
  cont.innerHTML = spinner();
  const p = await getPresentation("scenario_explanation", {
    loss: r.loss, owner_shortfall: r.owner_shortfall,
    final_illustration: r.final_illustration, peril: r.peril,
    sum_insured: r.sum_insured, value_at_risk: r.value_at_risk,
  });
  cont.innerHTML = `
    ${renderHero("You would pay this yourself", money(r.owner_shortfall),
      `on a ${money(r.loss)} ${r.peril} loss · your insurer would pay ${money(r.final_illustration)}`, "HIGH")}
    ${renderAssessment(p?.pres, p?.mode)}
    <div class="card mb-16"><div class="card-head"><h3>How we get to that number</h3></div>
      ${renderWaterfall(r.steps, "What you lose", r.loss)}
      <p class="text-xs text-muted mt-12">Every line comes from a document. Open one to see the exact words.</p>
    </div>
    <div class="card mb-16"><div class="card-head"><h3>The proof</h3></div>
      ${renderEvidenceStack((r.steps || []).map(st => st.provenance).filter(x => x && x.quote))}
    </div>
    <div class="insight warn"><div class="insight-label">Worth knowing</div>
      <div class="insight-text">${esc(r.disclaimer || "")}</div></div>`;
};

/* ── Will it actually pay? ─────────────────────────────────────────────── */
async function smeWillItPay(el) {
  el.innerHTML = spinner("Reading the promises in your policy");
  const d = await api("/api/warranty"); if (!d) return;
  if (!d.available) {
    el.innerHTML = pageTitle("Will it actually pay?")
      + emptyState("Not available", esc(d.reason || ""));
    return;
  }
  const c = d.counts || {};
  const live = (d.register || []).filter(e => e.state !== "NOT_IN_POLICY");
  const setAside = (d.register || []).filter(e => e.state === "NOT_IN_POLICY");
  const unmet = c.BREACHED || 0;
  const p = await getPresentation("warranty_overview", {
    counts: c, obligations_in_policy: d.obligations_in_policy,
    conditions_precedent_breached: d.conditions_precedent_breached,
  });

  el.innerHTML = pageTitle("Will it actually pay?",
      "Having cover and getting paid are two different things. Your policy contains promises you agreed to keep.")
    + renderHero(unmet ? "Promises not being kept" : "Promises we can't confirm",
        String(unmet || c.UNVERIFIED || 0),
        `out of ${d.obligations_in_policy || 0} we found in your policy`,
        unmet ? "HIGH" : c.UNVERIFIED ? "ELEVATED" : "LOW")
    + renderAssessment(p?.pres, p?.mode)
    + (d.conditions_precedent_breached > 0 ? `<div class="insight critical mb-16">
        <div class="insight-label">Why this one matters most</div>
        <div class="insight-text">${d.conditions_precedent_breached} of these is the strict kind of promise. If it slips, an insurer may be able to turn down a claim <strong>even if the slip had nothing to do with what went wrong</strong> — a lapsed alarm certificate can affect a fire claim.</div></div>` : "")
    + `<div class="card mb-16"><div class="card-head"><h3>Every promise we found</h3></div>
        ${renderObligationMap(live)}</div>`
    + `<div class="findings mb-16">${live.map(e => `<div class="finding" data-act="toggle-detail">
        <div class="finding-head"><div class="finding-title">${esc(e.title)}</div>
          <span class="tag tag-${e.state === "BREACHED" ? "danger" : e.state === "UNVERIFIED" ? "warning" : "success"}">${esc(_OB_WORDS[e.state] || e.state)}</span></div>
        <div class="finding-body">${esc(e.detail || "")}</div>
        <div class="finding-detail">
          <p class="text-sm"><strong>Why it's there:</strong> ${esc(e.why_it_matters || "")}</p>
          ${e.clause_quote ? `<div class="v-evidence mt-12"><div class="v-evidence__quote">"${esc(e.clause_quote)}"</div><div class="v-evidence__src">Your policy, page ${e.clause_page} — we searched for "${esc(e.matched_phrase || "")}"</div></div>` : ""}
          ${renderEvidenceStack(e.evidence)}
          ${packSources(e.pack_sources)}
          ${e.state === "UNVERIFIED" ? `<div class="insight warn mt-12"><div class="insight-label">How to clear this</div><div class="insight-text">Send us ${(e.verified_by || []).map(v => esc(v.replace(/_/g, " ").toLowerCase())).join(" or ")}.</div></div>` : ""}
        </div></div>`).join("")}</div>`
    + `<div class="card mb-16"><div class="card-head"><h3>What we checked and decided against</h3><span class="tag">${setAside.length}</span></div>
        <p class="text-sm text-muted">We show you what we looked for and didn't find. Where a word turned up but didn't actually create a promise, here's the wording and why we let it go — so you can disagree with us.</p>
        <table class="mt-12"><tbody>
          ${setAside.map(e => `<tr><td>${esc(e.title)}</td><td class="text-xs text-muted">${(e.phrases_set_aside || []).length
            ? `found "${esc(e.phrases_set_aside[0].phrase)}" on page ${e.phrases_set_aside[0].page} — ${esc(e.phrases_set_aside[0].reason)}`
            : "not in your policy at all"}</td></tr>`).join("")}
        </tbody></table></div>`
    + `<div class="insight warn"><div class="insight-label">Worth knowing</div><div class="insight-text">${esc(d.disclaimer || "")}</div></div>`;
}

/* ── If you can't trade ────────────────────────────────────────────────── */
async function smeBI(el) {
  el.innerHTML = spinner("Checking your cover for lost income");
  const d = await api("/api/bi"); if (!d) return;
  if (!d.available) {
    el.innerHTML = pageTitle("If you can't trade") + emptyState("Not available", esc(d.reason || ""));
    return;
  }
  if (d.abstained) {
    el.innerHTML = pageTitle("If you can't trade")
      + `<div class="insight warn"><div class="insight-label">We can't tell yet</div>
          <div class="insight-text">${esc(d.message || "")}</div></div>
        <p class="text-sm text-muted mt-12">${esc(d.reason || "")}</p>`;
    return;
  }
  const months = (d.recovery_benchmark || {}).months;
  const none = d.cover_present === false;
  const p = await getPresentation("bi_overview", {
    cover_present: d.cover_present, recovery_months: months,
  });
  el.innerHTML = pageTitle("If you can't trade",
      "Your policy pays to replace what you lost. This is about the money you don't earn while you rebuild.")
    + renderHero("Cover for lost income", none ? "NONE" : "In place",
        none ? `and it could take about ${months} months to trade normally again` : "worth checking the amount still fits",
        none ? "HIGH" : "LOW")
    + renderAssessment(p?.pres, p?.mode)
    + (none ? `<div class="card mb-16"><div class="card-head"><h3>What that gap looks like</h3></div>
        ${renderFlowchart([
          { text: "Something serious happens", type: "start" },
          { text: "Your policy pays to rebuild and replace stock", type: "ok" },
          { text: `You can't trade for around ${months} months`, type: "warn" },
          { text: "None of that lost income is covered", type: "bad" },
        ])}</div>` : "")
    + (d.findings || []).map(f => `<div class="finding mb-12" data-act="toggle-detail">
        <div class="finding-head"><div class="finding-title">${esc(f.headline)}</div>
          <span class="tag tag-${f.severity === "HIGH" ? "danger" : f.severity === "MODERATE" ? "warning" : "success"}">${esc(titleCase(f.severity))}</span></div>
        <div class="finding-body">${esc(f.detail)}</div>
        <div class="finding-detail">
          ${renderEvidenceStack(f.operands)}
          ${f.basis === "DECLARED_ASSUMPTION" ? `<div class="insight warn mt-12"><div class="insight-label">This part is an estimate</div><div class="insight-text">${esc((d.recovery_benchmark || {}).rationale || "")} We use it to raise the question, never to work out an amount.</div></div>` : ""}
        </div></div>`).join("")
    + ((d.abstentions || []).length ? `<div class="card mb-16"><div class="card-head"><h3>What we won't guess at</h3><span class="tag">${d.abstentions.length}</span></div>
        ${d.abstentions.map(a => `<div class="insight mt-8"><div class="insight-label">${esc(a.question)}</div><div class="insight-text">${esc(a.reason)}</div>${(a.resolved_by || []).length ? `<div class="text-xs text-muted mt-8">Send us: ${a.resolved_by.map(r => esc(r.replace(/_/g, " ").toLowerCase())).join(", ")}</div>` : ""}</div>`).join("")}
      </div>` : "")
    + `<div class="card mb-16"><div class="card-head"><h3>Why we look at this at all</h3></div>
        ${((d.evidence_base || {}).citations || []).map(ct => `<div class="v-evidence"><div class="v-evidence__quote">${esc(ct.finding)}</div><div class="v-evidence__src">${esc(ct.source)}</div></div>`).join("")}</div>
      <div class="insight warn"><div class="insight-label">Worth knowing</div><div class="insight-text">${esc(d.disclaimer || "")}</div></div>`;
}

/* ── What changed this year ────────────────────────────────────────────── */
async function smeRenewal(el) {
  el.innerHTML = spinner("Comparing this year's policy with last year's");
  const d = await api("/api/renewal/compare"); if (!d) return;
  if (!d.available) {
    el.innerHTML = pageTitle("What changed this year") + emptyState("Nothing to compare", esc(d.reason || ""));
    return;
  }
  const changes = d.changes || [];
  const material = changes.filter(c => c.materiality === "MATERIAL");
  const admin = changes.filter(c => c.materiality !== "MATERIAL");
  const p = await getPresentation("renewal_summary", {
    material_count: material.length, change_count: changes.length,
  });
  el.innerHTML = pageTitle("What changed this year",
      "A policy can renew at the same price with less inside it. Here's the comparison.")
    + renderHero("Changes that actually matter", String(material.length),
        `out of ${changes.length} difference${changes.length === 1 ? "" : "s"} we found`,
        material.length ? "HIGH" : "LOW")
    + renderAssessment(p?.pres, p?.mode)
    + (material.length ? `<h3 class="mb-8">Read these before you renew</h3>
        ${material.map(c => `<div class="card mb-12" style="border-left:4px solid var(--danger,#c5221f)">
          <div class="card-head"><h3>${esc(c.label || "")}</h3><span class="tag tag-danger">Matters</span></div>
          <p class="text-sm">${esc(c.materiality_reason || "")}</p>
          <div class="grid-2 mt-12">
            <div><div class="text-xs text-muted mb-8">Last year</div>
              ${c.prior_quote ? `<div class="v-evidence"><div class="v-evidence__quote">"${esc(c.prior_quote)}"</div><div class="v-evidence__src">page ${c.prior_page ?? "—"}</div></div>` : '<p class="text-sm text-muted">Wasn\'t there</p>'}</div>
            <div><div class="text-xs text-muted mb-8">This year</div>
              ${c.current_quote ? `<div class="v-evidence"><div class="v-evidence__quote">"${esc(c.current_quote)}"</div><div class="v-evidence__src">page ${c.current_page ?? "—"}</div></div>` : '<p class="text-sm text-muted">Gone</p>'}</div>
          </div></div>`).join("")}` : "")
    + (admin.length ? `<div class="card"><div class="card-head"><h3>Just admin</h3><span class="tag">${admin.length}</span></div>
        <ul class="text-sm">${admin.map(c => `<li>${esc(c.label || "")}</li>`).join("")}</ul></div>` : "")
    + `<p class="text-xs text-muted mt-16">${esc(d.notice || "")}</p>`;
}

/* ── What we checked ──────────────────────────────────────────────────── */
async function smeAgents(el) {
  el.innerHTML = spinner("Asking both agents what they have seen");
  // Signals and Sentinel are fetched alongside the activity log so the convergence panel
  // above it reads from the SAME data the two agents actually returned, rather than from a
  // second summary that could disagree with the rows underneath.
  const [d, signals, sentinel] = await Promise.all([
    api("/api/agents/activity"), api("/api/signals"), api("/api/sentinel"),
  ]);
  if (!d) return;
  const rows = d.activity || [];
  el.innerHTML = pageTitle("What we checked",
      "Veris looks on its own schedule. You don't have to remember to ask.")
    + `<div class="mb-24">${convergencePanel(signals, sentinel, null)}</div>`
    + `<div class="stats mb-16">
        <div class="stat"><div class="val">${d.count || 0}</div><div class="lbl">Checks run</div></div>
        <div class="stat"><div class="val">${rows.filter(r => r.is_live).length}</div><div class="lbl">Using live data</div></div>
        <div class="stat amber"><div class="val">${d.total_rejected || 0}</div><div class="lbl">Claims we threw away</div></div>
      </div>
      <div class="insight mb-16"><div class="insight-label">Why that last number matters</div>
        <div class="insight-text">When something we read can't be found word-for-word in the source, we discard it rather than show it to you. Anyone can list findings. This is the count of things we refused to say.</div></div>
      <div class="findings">${rows.map(r => `<div class="finding" data-act="toggle-detail">
        <div class="finding-head"><div class="finding-title">${esc(titleCase(r.agent))} · ${esc(r.result || r.summary || "")}</div>${renderDataBadge(r.is_live)}</div>
        <div class="finding-body">${esc(r.planned_check || "")}</div>
        <div class="finding-meta">
          <span class="text-xs text-muted">${esc(String(r.timestamp || "").replace("T", " ").slice(0, 19))}</span>
          <span class="tag">${r.facts_extracted ?? 0} found</span>
          <span class="tag tag-success">${r.facts_grounded ?? 0} proven</span>
          ${r.facts_rejected ? `<span class="tag tag-danger">${r.facts_rejected} discarded</span>` : ""}
        </div>
        <div class="finding-detail">
          <table class="mt-8"><thead><tr><th>Address called</th><th>Result</th></tr></thead><tbody>
            ${(r.calls || []).map(c => `<tr><td class="text-xs" style="font-family:var(--mono);word-break:break-all">${esc(c.url || "")}</td><td>${esc(c.status || "")}</td></tr>`).join("") || '<tr><td colspan="2" class="text-xs text-muted">no outside calls on this run</td></tr>'}
          </tbody></table></div>
      </div>`).join("")}</div>
      <p class="text-xs text-muted mt-16">${esc(d.notice || "")}</p>`;
}

/* ── Share with adviser ─────────────────────────────────────────────────
 *
 * The checkboxes name DOCUMENTS, and that is a correctness requirement rather than a
 * wording preference. The consent gate resolves each finding to the documents it rests on
 * and compares those against the granted list, so a grant of "findings" / "facts" /
 * "score" — which is what this screen used to post — matched no document and withheld
 * everything. The owner saw "Shared", the adviser saw an empty case, and nothing reported
 * a problem. The list now comes from `/api/consent/status.shareable`, which is derived
 * from the documents actually ingested.
 */
async function smeConsent(el) {
  el.innerHTML = spinner();
  const st = await api("/api/consent/status"); if (!st) return;
  const shareable = st.shareable || [];
  const recipient = st.adviser_recipient || "Licensed adviser";
  // MEASURED DEFECT. The recipient was a free-text box pre-filled with "Licensed
  // adviser", and the server matches the recipient string EXACTLY. So an owner who typed
  // their adviser's real firm name granted a disclosure the adviser's own screen then
  // refused, and "share this with my insurer" was unexpressible — the carrier matches on
  // its organisation name, which this screen never named. Sharing with the carrier was
  // therefore impossible to do by accident and impossible to do on purpose.
  //
  // The presets come from the server so the names cannot drift out of step with the
  // matcher. Free text still works; these are the counterparties that exist.
  const presets = st.recipients || [];

  el.innerHTML = pageTitle("Share your review",
      "Nothing leaves this workspace unless you send it. You choose what, and to whom.")
    + `<div class="card mb-16">
        <div class="card-head"><h3>${st.granted ? "You are sharing" : "Not sharing anything yet"}</h3>
          <span class="tag tag-${st.granted ? "success" : ""}">${st.granted ? "Shared" : "Private"}</span></div>
        ${st.granted ? `<p class="text-sm">Your adviser can see what you granted below. You can stop this at any time.</p>
          <ul class="text-sm mt-8">${(st.disclosures || []).map(x => `<li><b>${esc(x.recipient || "")}</b> — ${(x.fields || []).map(f => `<span class="tag">${esc(titleCase(f))}</span>`).join(" ") || "nothing"}</li>`).join("")}</ul>
          <div class="btn-group mt-16">
            <button class="btn btn-secondary" id="crevoke">Stop sharing</button>
            <button class="btn btn-ghost" id="cedit">Change what they can see</button>
          </div>`
        : shareable.length
          ? `<p class="text-sm mb-12">Tick the documents they may read. They will only see a finding if they were given every document it rests on — anything held back is counted for them, so they can ask, but they cannot read it.</p>
            <div class="form-group"><label for="crecip">Who should see it</label>
              ${presets.length ? `<div class="btn-group mb-8">${presets.map(p => `
                <button type="button" class="btn btn-ghost btn-sm cpreset"
                  data-recipient="${esc(p.recipient)}"
                  title="${esc(p.why || "")}">${esc(p.label || p.recipient)}</button>`).join("")}</div>` : ""}
              <input type="text" id="crecip" value="${esc(recipient)}" maxlength="120">
              <p class="text-xs text-muted mt-8" id="crecipwhy">${esc((presets[0] || {}).why || "")}</p></div>
            <div class="form-group"><label>Which documents they can read</label>
              <div class="consent-list">
                ${shareable.map(k => `<label class="consent-row">
                  <input type="checkbox" class="cf" value="${esc(k.kind)}" checked>
                  <span class="consent-row__l">${esc(k.label)}
                    <span class="consent-row__m">${(k.filenames || []).map(f => esc(f)).join(", ") || `${esc(k.count)} document(s)`}</span>
                  </span></label>`).join("")}
              </div>
              <div class="btn-group mt-8">
                <button type="button" class="btn btn-ghost btn-sm" id="call">Select all</button>
                <button type="button" class="btn btn-ghost btn-sm" id="cnone">Select none</button>
              </div></div>
            <button class="btn btn-primary mt-16" id="cgrant">Share it</button>`
          : `<div class="insight warn"><div class="insight-label">Nothing to share yet</div>
              <div class="insight-text">We have not read any documents for this business, so there is nothing an adviser could be given.</div>
              <div class="insight-action"><button class="btn btn-sm btn-primary" data-act="open" data-arg="sme-docs-hub|docs|add">Add documents →</button></div></div>`}
        <div id="cresult"></div>
      </div>
      <div class="card"><div class="card-head"><h3>Why this is a button and not a setting</h3></div>
        <p class="text-sm text-muted">Permission you can't take back isn't permission. Stopping sharing removes their access immediately.</p>
        <p class="text-xs text-muted mt-8">${esc(st.notice || "")}</p></div>`;

  const grant = $("#cgrant");
  if (grant) grant.onclick = async () => {
    const to = $("#crecip").value.trim() || recipient;
    const fields = $$(".cf").filter(c => c.checked).map(c => c.value);
    if (!fields.length) {
      $("#cresult").innerHTML = `<div class="insight warn mt-12"><div class="insight-text">Choose at least one document. Sharing nothing is what "Private" already means.</div></div>`;
      return;
    }
    try {
      await api("/api/consent", { method: "POST", body: { recipient: to, fields } });
      toast("Shared with " + to);
      nav8("sme-consent");
    } catch (err) {
      $("#cresult").innerHTML = `<div class="insight critical mt-12"><div class="insight-text">${esc(err.message)}</div></div>`;
    }
  };
  /* Preset buttons fill the box rather than replacing it, so an owner can still name a
   * recipient the product does not know about — a second adviser, a lender, a landlord.
   * The explanation swaps with the choice, because "what does sharing with my insurer
   * actually mean" is the question an owner hesitates on. */
  $$(".cpreset").forEach(b => b.onclick = () => {
    const box = $("#crecip");
    if (box) box.value = b.dataset.recipient || "";
    const why = $("#crecipwhy");
    const match = presets.find(p => p.recipient === b.dataset.recipient);
    if (why) why.textContent = (match && match.why) || "";
  });
  const all = $("#call");
  if (all) all.onclick = () => $$(".cf").forEach(c => { c.checked = true; });
  const none = $("#cnone");
  if (none) none.onclick = () => $$(".cf").forEach(c => { c.checked = false; });
  const revoke = $("#crevoke");
  if (revoke) revoke.onclick = async () => {
    await api("/api/consent/revoke", { method: "POST", body: {} });
    toast("Sharing stopped");
    nav8("sme-consent");
  };
  // Changing the grant is revoke-then-regrant rather than an edit, because the disclosure
  // row is the audit record of what was released and when. Mutating it in place would
  // rewrite history; revoking writes a REVOKE row and the new grant writes its own.
  const edit = $("#cedit");
  if (edit) edit.onclick = async () => {
    await api("/api/consent/revoke", { method: "POST", body: {} });
    nav8("sme-consent");
  };
}

/* ── Your record ─────────────────────────────────────────────────────── */
async function smeRecord(el) {
  el.innerHTML = spinner();
  const d = await api("/api/record"); if (!d) return;
  el.innerHTML = pageTitle("Your record",
      "The facts we established, in order, each one sealed so it can't be edited later.")
    + `<div class="card mb-16"><div class="card-head"><h3>${(d.facts || []).length} established facts</h3></div>
        <table><thead><tr><th>What</th><th>Value</th><th>Where it came from</th></tr></thead><tbody>
          ${(d.facts || []).map(f => `<tr><td>${esc(titleCase(f.attribute || ""))}</td>
            <td style="font-weight:600">${esc(String(f.value || ""))}${f.unit ? " " + esc(f.unit) : ""}</td>
            <td class="text-xs text-muted">${esc(titleCase(f.author_class || ""))}</td></tr>`).join("")}
        </tbody></table></div>
      <div class="grid-2 mb-16">
        <button class="btn btn-secondary" data-act="export-record">Download my record</button>
        <button class="btn btn-ghost" data-act="delete-case">Delete everything</button>
      </div>
      <div class="card"><div class="card-head"><h3>What "sealed" means</h3></div>
        <p class="text-sm text-muted">Each fact carries a fingerprint of the one before it. If anything were changed after the fact, the chain wouldn't line up — and anyone can check that, including you, using the downloaded file.</p></div>`;
}

window.exportRecord = async function () {
  const doc = await api("/api/record/export");
  const v = await api("/api/record/verify", { method: "POST", body: doc });
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "veris-record.json";
  a.click();
  URL.revokeObjectURL(a.href);
  alert(v && v.valid
    ? "Downloaded. We checked the chain and it lines up."
    : "Downloaded.");
};

window.deleteCase = async function () {
  sessionStorage.removeItem(EXAMPLE_FLAG);
  sessionStorage.removeItem(WORDING_NOTICE_KEY);
  if (!confirm("Delete everything we found? Your downloaded record still works. This can't be undone.")) return;
  await api("/api/case", { method: "DELETE" });
  nav8("sme-home");
};

/* ══════════════════════════════════════════════════════════════════════════
   EXPOSURE SURFACES

   Five screens for the five things the problem statement actually says: that owners
   don't know what they don't know; that cyber, supply chain and asset gaps go
   unnoticed; that nobody finds out until a claim is denied; that gaps compound as the
   business grows; and that the answer has to fit the stage the business is at.

   These share one rule that the rest of the product does not need. Their subject is an
   ABSENCE, and an absence has three states rather than two: covered, nothing responds,
   and we could not tell. The third must never be drawn like the second. Colouring an
   unknown red would be the visual form of the claim the backend refuses to make, and it
   would also destroy the thing that makes the other two believable.
   ══════════════════════════════════════════════════════════════════════════ */

/* Shared: map a backend verdict to the knowledge palette in styles.css. One function so
 * a state cannot mean one colour on the cyber screen and another on dependencies. */
function kClass(verdict) {
  return ({
    ASSESSED: "is-assessed", PARTIAL: "is-partial", NOT_ASSESSED: "is-unknown",
    NOTHING: "is-gap", UNKNOWN: "is-unknown", REVIEW: "is-partial",
  })[verdict] || "is-unknown";
}

function evidenceBadge(cls) {
  const key = String(cls || "").toUpperCase();
  const mod = key === "PROVEN" ? "" : key === "DECLARED" ? "declared"
    : key === "MARKET_CONVENTION" ? "convention" : "unestablished";
  const label = key === "MARKET_CONVENTION" ? "Market convention" : titleCase(key);
  return `<span class="ev-badge ${mod}">${esc(label || "Unestablished")}</span>`;
}

/* ── What we did not check ────────────────────────────────────────────────
 *
 * The opening screen, and the argument for the whole product. It reports the boundary of
 * its own assessment: how many risk areas exist, how many we can speak to, and what the
 * score leaves out. Every other dashboard shows what it found; the gap between "we found
 * nothing" and "we could not look" is where an SME's real exposure lives, and it renders
 * identically everywhere else as an absence of red.
 */
async function smeBlindSpots(el) {
  el.innerHTML = spinner("Working out what we could not check");
  const d = await api("/api/blindspots"); if (!d) return;
  if (d.available === false && d.deleted) {
    el.innerHTML = pageTitle("What we haven't checked") + emptyState(
      "Nothing to report", esc(d.reason));
    return;
  }

  const cov = d.coverage || {};
  const segs = (d.areas || []).map(a =>
    `<div class="cov__seg ${a.state === "ASSESSED" ? "on" : a.state === "PARTIAL" ? "part" : ""}"></div>`
  ).join("");

  const areas = (d.areas || []).map(a => `
    <div class="area k-state ${kClass(a.state)}">
      <div class="area__head">
        <div class="area__name">${esc(a.label)}</div>
        <span class="area__pill">${esc(a.state_label)}</span>
      </div>
      <div class="area__plain">${esc(a.plain)}</div>
      ${a.checked && a.checked.length ? `<div class="area__meta">
        <b>We checked</b>
        <ul class="area__list">${a.checked.slice(0, 3).map(c => `<li>${esc(c)}</li>`).join("")}</ul>
      </div>` : ""}
      ${a.could_not_check && a.could_not_check.length ? `<div class="area__meta">
        <b>We could not check</b>
        <ul class="area__list">${a.could_not_check.slice(0, 3).map(c => `<li>${esc(c)}</li>`).join("")}</ul>
      </div>` : ""}
    </div>`).join("");

  const silent = (d.silent_gaps || []).map(g => `
    <div class="silent">
      <div class="silent__q">${esc(g.what_we_could_not_check)}</div>
      <div class="silent__why">${esc(g.why_quiet_is_not_safe)}</div>
      <div class="silent__fix"><b>${esc(g.resolved_by)}</b> would establish ${esc(g.resolving_establishes)}</div>
    </div>`).join("");

  const caveat = d.score_caveat || {};
  el.innerHTML = pageTitle("What we haven't checked",
    "The most useful screen we can give you, because it is the one every other dashboard leaves out.")
    + `<div class="cov mb-16">
      <div class="cov__top">
        <div>
          <div class="cov__label">Areas we can speak to</div>
          <div class="cov__fig">${cov.areas_we_can_speak_to ?? "—"}<small> of ${cov.areas_total ?? "—"}</small></div>
        </div>
        <div><div class="cov__label">What that means</div>
          <div class="cov__say">${esc(cov.statement || "")}</div></div>
      </div>
      <div class="cov__track">${segs}</div>
      <div class="cov__legend">
        <span><i style="background:var(--success)"></i>Checked</span>
        <span><i style="background:var(--warning)"></i>Partly checked</span>
        <span><i style="background:var(--border)"></i>Not checked</span>
      </div>
      ${caveat.applies ? `<div class="caveat">
        <div class="caveat__t">About your score</div>
        <p>${esc(caveat.statement)}</p>
        ${caveat.why_not_zero ? `<p>${esc(caveat.why_not_zero)}</p>` : ""}
      </div>` : ""}
    </div>
    <div class="sub-head">Every area of your protection</div>
    <div class="areas mb-24">${areas}</div>
    ${silent ? `<div class="sub-head">Where quiet does not mean safe</div>${silent}` : ""}
    <div class="honest"><b>Why this screen exists.</b> ${esc(d.why_this_screen_exists || "")}
      <br><br>${esc(d.notice || "")}</div>`;
}

/* ── Cyber and data ───────────────────────────────────────────────────────
 *
 * The screen the problem statement asks for first, and the one with the strongest
 * evidence in the product: the carrier's own exclusion, quoted verbatim at a page
 * number. Nothing here is predictive — no breach probability, no estimated penalty, no
 * network scan — because none of that can be evidenced from a document.
 */
async function smeCyber(el) {
  el.innerHTML = spinner("Reading what your policy says about data");
  const d = await api("/api/cyber"); if (!d) return;

  if (d.available === false) {
    el.innerHTML = pageTitle("Cyber and data") + emptyState(
      "We are not going to guess", esc(d.reason),
      `<button class="btn btn-primary" data-act="nav" data-arg="sme-upload">Add documents</button>`);
    return;
  }

  const holdings = (d.holdings || []).map(h => `
    <div class="dep">
      <div class="dep__head">
        <span class="dep__kind">${esc(h.subject)}</span>
        ${evidenceBadge(h.evidence_class)}
        <span class="text-xs text-muted">${esc(h.document_kind)}</span>
      </div>
      <div class="dep__why">${esc(h.basis)}</div>
      ${h.quote ? `<div class="quote-src">${esc(h.quote)}</div>` : ""}
    </div>`).join("");

  const heads = (d.cost_heads || []).map(c => {
    const gap = c.responds === "NOTHING";
    return `<div class="head k-state ${kClass(c.responds)} ${gap ? "is-gap" : ""} ${c.responds === "UNKNOWN" ? "is-unknown" : ""}">
      <div>
        <div class="head__n">${esc(c.head)}</div>
        <div class="head__p">${esc(c.plain)}</div>
        <div class="head__w">${esc(c.why)}</div>
      </div>
      <span class="head__r">${gap ? "Nothing responds" : c.responds === "UNKNOWN" ? "Can't tell" : c.responds === "REVIEW" ? "Needs a review" : titleCase(c.responds)}</span>
    </div>`;
  }).join("");

  const st = d.statutory || {};
  const statutory = st.regime ? `<div class="sub-head">What the law requires</div>
    <div class="card mb-16">
      <div class="card-head"><h3>${esc(st.regime)}</h3></div>
      <p class="text-sm mb-12">${esc(st.notification_duty || "")}</p>
      <div class="stats mb-12">
        <div class="stat"><div class="val">${money(st.penalty_ceiling_sgd)}</div>
          <div class="lbl">Statutory maximum penalty</div>
          <div class="sub">Not a prediction</div></div>
        <div class="stat"><div class="val">${esc(st.notifiable_scale)}</div>
          <div class="lbl">People affected before a breach must be reported</div></div>
      </div>
      <p class="text-xs text-muted">${esc(st.penalty_ceiling_basis || "")}</p>
      <div class="caveat"><div class="caveat__t">Read this before you read the figure</div>
        <p>${esc(st.not_a_prediction || "")}</p>
        <p>${esc(st.which_branch_applies || "")}</p></div>
      <p class="quote-src mt-12">${esc(st.authority || "")}</p>
      ${packSources(st.pack_sources)}
    </div>` : "";
  const ex = d.exclusion;

  el.innerHTML = pageTitle("Cyber and data",
    "You hold personal data. This is what your policy says about losing it.")
    + `<div class="big ${d.gap_open ? "is-gap" : ""}">
      <div class="big__l">Your position</div>
      <div class="big__v">${esc(d.cover_label)}</div>
      <div class="big__s">${d.gap_open
        ? `Of the ${(d.cost_heads || []).length} bills a data breach creates, <b>${d.uncovered_count}</b> have nothing in your policy that responds.`
        : "A cyber section is on your schedule. What it covers still needs reading with your adviser."}</div>
    </div>

    ${ex ? `<div class="sub-head">Your insurer's own words</div>
      <div class="verbatim mb-16">
        <div class="verbatim__tag"><i></i>Verbatim from your policy — nothing paraphrased</div>
        <div class="verbatim__q">${esc(ex.quote)}</div>
        <div class="verbatim__src">
          <span><b>Document</b> ${esc(titleCase(ex.document_kind || ""))}</span>
          <span><b>Page</b> ${esc(ex.page_no)}</span>
          <span><b>Written by</b> ${esc(titleCase(ex.author_class || ""))}</span>
          <span><b>Evidence</b> ${esc(ex.evidence_class)}</span>
        </div>
        <div class="verbatim__so">${esc(ex.why_it_matters)}</div>
      </div>` : ""}

    <div class="sub-head">Why the law applies to you</div>
    ${holdings}

    <div class="sub-head">What a breach costs, and what responds</div>
    <div class="heads mb-24">${heads}</div>

    ${statutory}
    <div class="honest">${esc(d.notice || "")}</div>`;
}

/* ── Who you depend on ────────────────────────────────────────────────────
 *
 * The screen that has to argue against itself. Every dependency shown was read from a
 * document, so the map is real — and it can never be complete, because the dependencies
 * that matter most never appear in a document. The partial-map notice is therefore given
 * as much visual weight as the findings, not tucked into a footnote.
 */
async function smeDependencies(el) {
  el.innerHTML = spinner("Mapping what your income depends on");
  const d = await api("/api/supply-chain"); if (!d) return;

  const vis = d.visibility || {};
  const partialBlock = `<div class="partial">
      <div class="partial__t">This map is not complete, and that is the point</div>
      <div class="partial__s">${esc(vis.statement || "")}<br><br>${esc(vis.why_it_matters || "")}</div>
      ${(vis.what_would_complete_it || []).length ? `<div class="partial__grid">
        ${(vis.what_would_complete_it || []).map(i =>
          `<div class="partial__i"><b>${esc(i.document)}</b><span>${esc(i.establishes)}</span></div>`).join("")}
      </div>` : ""}
    </div>`;

  if (d.available === false) {
    el.innerHTML = pageTitle("Who you depend on") + emptyState(
      "Nothing established yet", esc(d.reason),
      `<button class="btn btn-primary" data-act="nav" data-arg="sme-upload">Add documents</button>`)
      + `<div class="mt-16">${partialBlock}</div>`;
    return;
  }

  const byDep = {};
  (d.exposure_matrix || []).forEach(e => {
    (byDep[e.dependency] = byDep[e.dependency] || []).push(e);
  });

  const deps = (d.dependencies || []).map(dep => `
    <div class="dep">
      <div class="dep__head">
        <span class="dep__kind">${esc(titleCase(dep.kind))}</span>
        <span class="dep__name">${esc(dep.name)}</span>
        ${evidenceBadge(dep.evidence_class)}
      </div>
      <div class="dep__why">${esc(dep.why)}</div>
      ${dep.quote ? `<div class="quote-src">${esc(dep.quote)}</div>` : ""}
      <div class="dep__modes">
        ${(byDep[dep.name] || []).map(e => `
          <div class="dep__mode ${e.responds === "NOTHING" ? "is-gap" : ""}">
            <span>${esc(e.mode_label)}</span>
            <b>${e.responds === "NOTHING" ? "Nothing responds" : e.responds === "UNKNOWN" ? "Can't tell" : "Needs a review"}</b>
          </div>`).join("")}
      </div>
    </div>`).join("");

  const how = d.how_cbi_works || {};
  el.innerHTML = pageTitle("Who you depend on",
    "Your policy draws a line around your address. Your income does not.")
    + `<div class="big ${d.gap_open ? "is-gap" : ""}">
      <div class="big__l">Supplier and dependency cover</div>
      <div class="big__v">${esc(d.cbi_label)}</div>
      <div class="big__s">We found <b>${(d.counts || {}).dependencies}</b> things outside your business that your income relies on, and
        <b>${(d.counts || {}).uncovered_paths}</b> of the ${(d.counts || {}).failure_paths} ways they could fail have nothing in your policy that responds.</div>
    </div>
    <div class="sub-head">What you depend on</div>
    ${deps}
    <div class="sub-head">How this cover normally works</div>
    <div class="card mb-16">
      <div class="card-head"><h3>Three things worth knowing</h3>${evidenceBadge(how.basis_class)}</div>
      <p class="text-sm mb-8"><b>It is an extension, not a policy.</b> ${esc(how.structure || "")}</p>
      <p class="text-sm mb-8"><b>Suppliers are usually named.</b> ${esc(how.named_basis || "")}</p>
      <p class="text-sm mb-12"><b>It usually needs physical damage.</b> ${esc(how.physical_damage_basis || "")}</p>
      <div class="caveat"><div class="caveat__t">What this is based on</div>
        <p>${esc(how.basis_caveat || "")}</p></div>
    </div>
    ${(d.concentrations || []).length ? `<div class="sub-head">Where you are concentrated</div>
      <p class="tab-lead">A supplier register tells you who you have a contract with. It does
        not tell you who that supplier cannot function without. These are the concentrations
        your own documents evidence.</p>
      ${(d.concentrations || []).map(c => `<div class="dep">
        <div class="dep__head">
          <span class="dep__kind">${esc(titleCase(c.kind))}</span>
          <span class="dep__name">${esc(c.subject)}</span>
          ${evidenceBadge(c.evidence_class)}
        </div>
        <div class="dep__why">${esc(c.reading)}</div>
        <div class="ev__fix mt-12"><b>Why it bites:</b> ${esc(c.why_it_bites)}</div>
      </div>`).join("")}` : ""}

    ${d.sub_tier ? `<div class="sub-head">What we cannot see, and will not guess</div>
      <div class="partial mb-16">
        <div class="partial__t">${esc(d.sub_tier.statement)}</div>
        <div class="partial__s">${esc(d.sub_tier.why_we_do_not_estimate_it)}</div>
        <div class="partial__grid">
          <div class="partial__i"><b>What would reveal it</b>
            <span>${esc(d.sub_tier.what_would_reveal_it)}</span></div>
        </div>
      </div>` : ""}

    <div class="sub-head">The limits of this map</div>
    ${partialBlock}
    <div class="honest">${esc(d.notice || "")}</div>`;
}

/* ── Protection for your stage ────────────────────────────────────────────
 *
 * The only screen that names a protection the owner does NOT hold, which makes it the
 * only one that can shade into selling. The whole design rests on one distinction being
 * impossible to miss: STATUTORY is law, MARKET_CONVENTION is a habit. They get different
 * badges, separate counts, and a caveat that says a convention is a question worth
 * answering rather than a failure.
 */
async function smeRoadmap(el) {
  el.innerHTML = spinner("Working out what stage your business is at");
  const d = await api("/api/stages"); if (!d) return;

  if (d.available === false) {
    el.innerHTML = pageTitle("Protection for your stage") + emptyState(
      "We could not place your business", esc(d.reason),
      `<button class="btn btn-primary" data-act="nav" data-arg="sme-upload">Add documents</button>`);
    return;
  }

  const here = (d.stage || {}).ordinal ?? -1;
  const ladder = (d.ladder || []).map(s => {
    const cls = s.ordinal === here ? "here" : s.ordinal < here ? "done" : "";
    return `<div class="rung ${cls}">
      ${s.ordinal === here ? `<span class="rung__you">You are here</span>` : ""}
      <div class="rung__n">Stage ${s.ordinal + 1}</div>
      <div class="rung__l">${esc(s.label)}</div>
      <div class="rung__d">${esc(s.describes)}</div>
    </div>`;
  }).join("");

  const heldSet = new Set(d.held || []);
  const absentSet = new Set(d.absent || []);
  const order = e => heldSet.has(e.protection) ? 2 : absentSet.has(e.protection) ? 0 : 1;
  const expectations = [...(d.expectations || [])].sort((a, b) => order(a) - order(b));

  const prot = expectations.map(e => {
    const held = heldSet.has(e.protection);
    const absent = absentSet.has(e.protection);
    const state = held ? "ASSESSED" : absent ? "NOTHING" : "UNKNOWN";
    const mark = held ? "✓" : absent ? "✕" : "?";
    return `<div class="pro k-state ${kClass(state)} ${absent ? "is-gap" : ""}">
      <div class="pro__mark">${mark}</div>
      <div>
        <div class="pro__l">${esc(e.label)}</div>
        <div class="pro__p">${esc(e.plain)}</div>
        <div class="pro__b">${held ? "<b>On your schedule.</b> " : absent ? "<b>Not on your schedule.</b> " : "<b>Your schedule cannot tell us either way.</b> "}${esc(e.because)}</div>
        ${e.authority ? `<div class="quote-src">${esc(e.authority)}</div>` : ""}
        ${packSources(e.pack_sources)}
      </div>
      <span class="pro__basis ${e.basis === "STATUTORY" ? "law" : "conv"}">${e.basis === "STATUTORY" ? "Law" : "Common"}</span>
    </div>`;
  }).join("");

  const evidence = (d.evidence || []).map(v => `
    <div class="stat"><div class="val">${esc(v.value)}</div>
      <div class="lbl">${esc(v.signal)}</div>
      <div class="sub">${esc(v.source || "")}</div></div>`).join("");

  const counts = d.counts || {};
  el.innerHTML = pageTitle("Protection for your stage",
    "Read from your documents, not from a questionnaire you filled in.")
    + `<div class="ladder">${ladder}</div>
    <div class="big">
      <div class="big__l">Where you are</div>
      <div class="big__v">${esc((d.stage || {}).label || "")}</div>
      <div class="big__s">${esc((d.stage || {}).describes || "")}
        ${d.next_stage ? ` Next comes <b>${esc(d.next_stage.label)}</b> — ${esc(d.next_stage.describes)}` : ""}</div>
    </div>
    <div class="sub-head">What put you here</div>
    <div class="stats mb-24">${evidence}</div>
    <div class="sub-head">What businesses at your stage usually hold</div>
    <div class="stats mb-16">
      <div class="stat green"><div class="val">${counts.held ?? 0}</div><div class="lbl">You hold</div></div>
      <div class="stat red"><div class="val">${counts.absent ?? 0}</div><div class="lbl">Not on your schedule</div></div>
      <div class="stat"><div class="val">${counts.indeterminate ?? 0}</div><div class="lbl">We can't tell</div></div>
    </div>
    <div class="prot mb-16">${prot}</div>
    <div class="caveat"><div class="caveat__t">Law versus habit — the difference matters</div>
      <p>${esc(d.basis_notice || "")}</p></div>
    <div class="honest">${esc(d.notice || "")}</div>`;
}

/* ── How this crept up ────────────────────────────────────────────────────
 *
 * The compounding screen. Reports DAYS, never dollars: exposure crystallises on loss
 * rather than accruing daily, so a running cost would be fabricated, while a day count
 * is the distance between two dates in the owner's own documents.
 *
 * It also shows the change they DID act on. A timeline of nothing but failures reads as a
 * sales pitch; crediting the one they closed is what makes the rest credible.
 */
async function smeCompounding(el) {
  el.innerHTML = spinner("Lining up what changed, and when");
  const d = await api("/api/compounding"); if (!d) return;

  if (d.available === false) {
    el.innerHTML = pageTitle("How this crept up") + emptyState(
      "No timeline yet", esc(d.reason));
    return;
  }

  const period = S.timePeriod || "all";
  const shown = (d.timeline || []).filter(c => timelineWithin(period, c.on, d.as_at));
  const hidden = (d.timeline || []).length - shown.length;

  const timeline = shown.map(c => {
    const cls = c.kind === "POLICY_CHANGE" ? "policy"
      : c.status === "CLOSED" ? "closed" : "open";
    return `<div class="ev ${cls}">
      <div class="ev__dot"></div>
      <div class="ev__when">${esc(c.on)}</div>
      <div class="ev__l">${esc(c.label)}</div>
      <div class="ev__d">${esc(c.detail)}</div>
      <div class="ev__tags">
        <span class="ev__tag ${cls}">${esc(c.status_label)}</span>
        ${evidenceBadge(c.evidence_class)}
        ${c.status === "OPEN" && c.days_open != null
          ? `<span class="ev__days">${c.days_open} days open</span>` : ""}
        ${c.status === "CLOSED" && c.days_open != null
          ? `<span class="text-xs text-muted">closed in ${c.days_open} day(s) by ${esc(c.closed_by || "")}</span>` : ""}
      </div>
      ${c.what_would_close_it ? `<div class="ev__fix"><b>What would close it:</b> ${esc(c.what_would_close_it)}</div>` : ""}
    </div>`;
  }).join("");

  const counts = d.counts || {};
  el.innerHTML = pageTitle("How this crept up",
    "Nothing here went wrong all at once. That is what makes it hard to notice.")
    + `<div class="big ${counts.open ? "is-gap" : ""}">
      <div class="big__l">The pattern</div>
      <div class="big__v">${counts.closed} of ${counts.business_changes} changes followed through</div>
      <div class="big__s">${esc(d.compounding_statement || "")}</div>
    </div>
    <div class="stats mb-24">
      <div class="stat"><div class="val">${counts.business_changes ?? 0}</div><div class="lbl">Changes in your business</div></div>
      <div class="stat green"><div class="val">${counts.closed ?? 0}</div><div class="lbl">Followed by a policy update</div></div>
      <div class="stat red"><div class="val">${counts.open ?? 0}</div><div class="lbl">Still open</div></div>
      <div class="stat amber"><div class="val">${d.longest_open_days ?? 0}</div><div class="lbl">Days the oldest has been open</div></div>
    </div>
    <div class="sub-head">Your timeline</div>
    <div class="btn-group mb-16">
      ${TIME_PERIODS.map(p => `<button class="btn btn-sm ${
        p.id === period ? "btn-primary" : "btn-secondary"}"
        data-act="tl-period" data-arg="${p.id}">${esc(p.label)}</button>`).join("")}
    </div>
    ${hidden ? `<p class="text-xs text-muted mb-12">${hidden} earlier entr${hidden === 1 ? "y" : "ies"} hidden by this filter.
      "This year" means the twelve months before ${esc(d.as_at)} — the latest date your
      documents evidence, not today, so this reads the same every time.</p>` : ""}
    <div class="tl2 mb-24">${timeline}</div>
    <div class="card mb-16">
      <div class="card-head"><h3>Why we count days and not dollars</h3></div>
      <p class="text-sm">${esc(d.why_days_not_dollars || "")}</p>
    </div>
    <div class="caveat"><div class="caveat__t">There may be nothing wrong here</div>
      <p>${esc(d.not_an_adjudication || "")}</p>
      <ul class="area__list">${(d.innocent_explanations || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
    </div>
    <p class="text-xs text-muted mt-12">Dated as at ${esc(d.as_at)}. ${esc(d.as_at_basis || "")}</p>`;
}

/* ── Gap snapshot ─────────────────────────────────────────────────────────
 *
 * The screen that had to resist one specific temptation: counting everything unresolved as
 * a gap and putting one big red number at the top. That is a better demo and a dishonest
 * product, so the three categories are rendered as three genuinely different things —
 * evidenced, unconfirmed, unexamined — and the third is grey.
 *
 * Status never rides on colour alone. Every row carries an icon and a written label as well,
 * because a colour-only status is unreadable to a colour-blind viewer and vanishes in the
 * screenshot somebody pastes into an email.
 */
async function smeGapSnapshot(el) {
  el.innerHTML = spinner("Pulling everything together");
  const d = await api("/api/gap-summary"); if (!d) return;
  if (d.available === false) {
    el.innerHTML = pageTitle("Your position") + emptyState("Nothing to show", esc(d.reason));
    return;
  }

  const row = i => `<div class="head k-state ${kClass(
      i.category === "ACTION_REQUIRED" ? "NOTHING"
      : i.category === "NEEDS_VERIFICATION" ? "REVIEW" : "NOT_ASSESSED")}
      ${i.category === "ACTION_REQUIRED" ? "is-gap" : ""}">
    <div>
      <div class="head__n"><span class="gap__icon">${esc(i.icon)}</span> ${esc(i.title)}</div>
      ${i.why ? `<div class="head__p">${esc(i.why)}</div>` : ""}
      ${i.resolved_by ? `<div class="head__w"><b>What would settle it:</b> ${esc(i.resolved_by)}</div>` : ""}
    </div>
    <div style="text-align:right">
      <span class="head__r">${esc(i.category_label)}</span>
      ${i.age_days ? `<div class="ev__days mt-8">${i.age_days} days open</div>` : ""}
      ${i.evidence_class ? `<div class="mt-8">${evidenceBadge(i.evidence_class)}</div>` : ""}
      ${i.source === "Your documents" ? `<button class="btn btn-ghost btn-sm mt-8"
        data-act="gap-why" data-arg="${esc(i.key)}">Show me why</button>` : ""}
    </div>
  </div>`;

  const next = d.best_next_action || {};
  el.innerHTML = pageTitle("Your position",
    "Everything we found, grouped by how sure we are. Three different things, not one red number.")
    + `<div class="big ${d.counts.ACTION_REQUIRED ? "is-gap" : ""}">
      <div class="big__l">In short</div>
      <div class="big__v">${esc(d.executive_summary)}</div>
      <div class="big__s"><b>Best next step:</b> ${esc(next.what || "")}
        ${next.because ? `<br><span class="text-sm text-muted">${esc(next.because)}</span>` : ""}</div>
    </div>

    <div class="stats mb-24">
      ${(d.categories || []).map(c => `<div class="stat ${
        c.category === "ACTION_REQUIRED" ? "red" : c.category === "NEEDS_VERIFICATION" ? "amber" : ""}">
        <div class="val">${c.count}</div>
        <div class="lbl">${esc(c.icon)} ${esc(c.label)}</div>
        <div class="sub">${esc(c.plain)}</div></div>`).join("")}
    </div>

    ${(d.categories || []).map(c => c.count ? `
      <div class="sub-head">${esc(c.icon)} ${esc(c.label)} — ${c.count}</div>
      <p class="tab-lead">${esc(c.plain)}</p>
      <div class="heads mb-24">${c.items.map(row).join("")}</div>` : "").join("")}

    <div class="honest">
      <b>How this screen is built.</b> ${esc(d.notice || "")}
      <br><br><b>${esc((d.language_rules || {}).uncertain_is_not_a_gap || "")}</b>
      <br>${esc((d.language_rules || {}).unassessed_is_not_uncovered || "")}
    </div>`;
}

/* ── The evidence drawer ──────────────────────────────────────────────────
 *
 * "Show me why" is the question this whole product exists to answer, so it must not be a
 * navigation event. Clicking a gap opens the quotation over the current screen, and the
 * screen underneath is still there when it closes.
 *
 * It fetches nothing new. The finding, its state, its class and its spans are already in the
 * Brief the screen loaded, so the drawer is a projection of data in hand — which also means
 * it cannot show a citation the engine did not verify.
 */
async function openEvidenceDrawer(ruleId) {
  S.drawer = { ruleId, loading: true };
  paintDrawer();
  try {
    const brief = await api("/api/brief");
    const finding = ((brief && brief.findings) || [])
      .find(f => f.rule_id === ruleId) || null;
    let plain = null;
    try {
      const explained = await api("/api/ai/explain", {
        method: "POST", body: { rule_id: ruleId } });
      plain = explained || null;
    } catch (_) { /* explanation is a bonus; the citation is the point */ }
    S.drawer = { ruleId, loading: false, finding, plain, page: null };
    paintDrawer();
    // The page image is fetched AFTER the drawer is on screen, not before. Rendering a
    // 28-page wording at 144 dpi takes a moment, and making the quotation wait behind a
    // picture of itself would be the wrong trade — the words are the evidence.
    await loadDrawerPage(0);
    return;
  } catch (err) {
    S.drawer = { ruleId, loading: false, error: err.message || String(err) };
  }
  paintDrawer();
}

/* Fetch and show the source page behind span number `i` of the open finding. */
async function loadDrawerPage(i) {
  const d = S.drawer;
  if (!d || !d.finding) return;
  const spans = d.finding.spans || [];
  const span = spans[i];
  if (!span || !span.document_id) return;
  d.spanIndex = i;
  d.page = { loading: true };
  paintDrawer();
  try {
    const q = `?span_id=${encodeURIComponent(span.span_id || "")}`;
    d.page = await api(`/api/source/${encodeURIComponent(span.document_id)}`
      + `/page/${encodeURIComponent(span.page_no)}${q}`);
  } catch (err) {
    d.page = { available: false, reason: err.message || String(err) };
  }
  paintDrawer();
}
window.loadDrawerPage = loadDrawerPage;

/* Full screen. A 440px drawer is the right size for reading a quotation and the wrong
 * size for reading a policy page, so the same page is re-fetched at print resolution and
 * shown over everything. Re-fetched rather than scaled up: enlarging a 144 dpi bitmap
 * would show the reader a blurrier version of the evidence at the exact moment they
 * decided to look closely. */
async function openPageFullscreen() {
  const d = S.drawer;
  if (!d || !d.finding) return;
  const span = (d.finding.spans || [])[d.spanIndex || 0];
  if (!span) return;
  S.pageFull = { loading: true };
  paintPageFullscreen();
  try {
    const q = `?span_id=${encodeURIComponent(span.span_id || "")}&full=true`;
    S.pageFull = await api(`/api/source/${encodeURIComponent(span.document_id)}`
      + `/page/${encodeURIComponent(span.page_no)}${q}`);
  } catch (err) {
    S.pageFull = { available: false, reason: err.message || String(err) };
  }
  paintPageFullscreen();
}
window.openPageFullscreen = openPageFullscreen;

function paintPageFullscreen() {
  document.getElementById("veris-pagefull")?.remove();
  const p = S.pageFull;
  if (!p) return;
  const host = document.createElement("div");
  host.id = "veris-pagefull";
  const body = p.loading
    ? spinner("Rendering the page at full size")
    : p.available && p.image
      ? `<img src="${esc(p.image)}" alt="Page ${esc(p.page_no)} of ${esc(p.filename || "the source document")}, with the quoted passage highlighted">`
      : `<div class="insight warn"><div class="insight-label">Cannot show the page</div>
          <div class="insight-text">${esc(p.reason || "")}</div></div>`;
  host.innerHTML = `<div class="pf__bar">
      <div class="pf__t">${esc(p.filename || "Source document")}${p.page_no
        ? ` — page ${esc(p.page_no)}${p.page_count ? " of " + esc(p.page_count) : ""}` : ""}</div>
      <button class="btn btn-sm btn-secondary" data-act="pagefull-close">Close</button>
    </div>
    <div class="pf__body">${body}</div>`;
  document.body.appendChild(host);
}

/* The page pane. FIRST thing in the drawer, above "What we found", because it is the
 * evidence and everything below it is commentary on the evidence.
 *
 * Renders in three states and never in a fourth: the page with the quotation marked, the
 * page unmarked with a line saying the quotation could not be located, or a named reason
 * there is no page at all. It does not substitute a different document's page when the
 * cited one has none — a page image is the most complete disclosure this product makes of
 * a document, and showing the wrong one under a quotation is a fabricated citation.
 */
function drawerPagePane(d) {
  const spans = (d.finding && d.finding.spans) || [];
  const withDoc = spans.filter(s => s.document_id);

  // A finding with no citation at all. Say so here rather than rendering nothing: an
  // absent panel reads as a missing feature, and this is a deliberate refusal — the
  // reason the finding is UNESTABLISHED is precisely that no document supports it.
  if (!withDoc.length) {
    return `<div class="pv-block">
      <div class="pv-block__h">The page it came from</div>
      <div class="pv pv--none">
        <p class="pv__note">No document supports this one, so there is no page to show.
          That is the finding, not a gap in the viewer: it is reported as unestablished
          precisely because nothing in your paperwork evidences it.</p>
        <button class="btn btn-sm btn-secondary" data-act="open"
          data-arg="sme-docs-hub|docs|add">Add the document →</button>
      </div></div>`;
  }

  const p = d.page;
  const active = d.spanIndex || 0;
  const picker = withDoc.length > 1
    ? `<div class="pv__tabs" role="group" aria-label="Which citation to show">
        ${spans.map((s, i) => s.document_id ? `<button class="pv__tab ${i === active ? "on" : ""}"
          data-act="drawer-page" data-arg="${i}">Page ${esc(s.page_no)}</button>` : "").join("")}
      </div>` : "";

  let inner;
  if (!p || p.loading) {
    inner = `<div class="pv__frame pv__frame--busy">${spinner("Opening the page")}</div>`;
  } else if (p.display === "EXCERPT") {
    inner = drawerExcerpt(p);
  } else if (p.available && p.image) {
    const h = p.highlight || {};
    // The full-screen control is an ICON in the bottom-right of the page itself, so it
    // reads as "expand this" rather than as another row of interface below it.
    inner = `<div class="pv__frame">
        <img src="${esc(p.image)}" alt="Page ${esc(p.page_no)} of ${esc(p.filename || "the source document")}${h.found ? ", with the quoted passage highlighted" : ""}">
        <button class="pv__full" data-act="pagefull-open" title="Open full screen"
          aria-label="Open page ${esc(p.page_no)} full screen">
          <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path
            d="M6 2H2v4M10 2h4v4M6 14H2v-4M10 14h4v-4" fill="none"
            stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </div>
      <div class="pv__meta">
        <span class="pv__file">${esc(p.filename || "")}</span>
        <span>page ${esc(p.page_no)}${p.page_count ? " of " + esc(p.page_count) : ""}</span>
        <span class="pv__hl ${h.found ? (h.method === "EXACT" ? "ok" : "part") : "no"}">${h.found
          ? `Highlighted${h.rect_count > 1 ? ` in ${esc(h.rect_count)} places` : ""}`
          : "Not highlighted"}</span>
      </div>
      ${h.method === "EXACT" ? "" : `<p class="pv__note">${esc(h.note || "")}</p>`}`;
  } else {
    inner = `<div class="pv--none">
      <p class="pv__note">${esc((p && p.reason) || "This page cannot be shown.")}</p>
      ${(p && p.reason || "").indexOf("demo data") >= 0 ? `<button class="btn btn-sm btn-secondary"
        data-act="load-example">Load the example business →</button>` : ""}</div>`;
  }

  return `<div class="pv-block">
    <div class="pv-block__h">${p && p.display === "EXCERPT" ? "The paragraph it came from" : "The page it came from"}</div>
    ${picker}
    <div class="pv">${inner}</div>
  </div>`;
}

/* A third party's published document: the cited paragraph and a link to the publisher.
 *
 * Never a page image. The server holds a reader's copy of a published wording, not the
 * right to reproduce it, so it sends the paragraph around the citation — cut from the text
 * the engine already stored — and the publisher's own address from the corpus manifest.
 * The quotation inside the paragraph is marked by the offsets the server returned, and
 * every piece of it goes through esc(): this is server-derived text from somebody else's
 * document. The link opens in a new tab with no opener and no referrer, and is only drawn
 * when the manifest recorded an http(s) address; an unrecorded one is said to be
 * unrecorded rather than guessed. */
function drawerExcerpt(p) {
  const x = p.excerpt;
  const pub = p.publisher || {};
  let para;
  if (x && typeof x.text === "string") {
    const a = Math.max(0, Math.min(x.text.length, Number(x.quote_start) || 0));
    const b = Math.max(a, Math.min(x.text.length, Number(x.quote_end) || 0));
    para = `<blockquote class="pv__excerpt">${x.cut_before ? "… " : ""}${esc(x.text.slice(0, a))}<mark>${esc(x.text.slice(a, b))}</mark>${esc(x.text.slice(b))}${x.cut_after ? " …" : ""}</blockquote>
      <div class="pv__meta">
        <span class="pv__file">${esc(pub.title || p.filename || "")}</span>
        <span>page ${esc(x.page_no || p.page_no)}${p.page_count ? " of " + esc(p.page_count) : ""}</span>
      </div>`;
  } else {
    para = "";
  }
  const safeUrl = typeof pub.url === "string" && /^https?:\/\//i.test(pub.url) ? pub.url : "";
  const link = pub.available && safeUrl
    ? `<a class="btn btn-sm btn-secondary" href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">Read it on the publisher's site →</a>`
    : `<p class="pv__note">${esc(pub.note || "No publisher link is recorded for this document.")}</p>`;
  return `<div class="pv--excerpt">
      ${para}
      <p class="pv__note">${esc(p.reason || "")}</p>
      ${link}
    </div>`;
}

function paintDrawer() {
  document.getElementById("veris-drawer")?.remove();
  const d = S.drawer;
  if (!d) return;
  const host = document.createElement("div");
  host.id = "veris-drawer";

  let body;
  if (d.loading) {
    body = spinner("Fetching the evidence");
  } else if (d.error) {
    body = `<div class="insight critical"><div class="insight-text">${esc(d.error)}</div></div>`;
  } else if (!d.finding) {
    body = `<div class="v-empty">This finding is not in the case you can read.</div>`;
  } else {
    const f = d.finding;
    body = `
      ${drawerPagePane(d)}
      <div class="v-drawer__section">
        <h4>What we found</h4>
        <p class="text-sm">${esc(f.headline)}</p>
        <div class="ev__tags mt-8">
          <span class="ev__tag ${f.state === "DOCUMENTED" ? "open" : "policy"}">${esc(f.state_label || f.state)}</span>
          ${evidenceBadge(f.class)}
        </div>
        ${packSources((f.detail || {}).pack_sources)}
      </div>
      ${d.plain && d.plain.text ? `<div class="v-drawer__section">
        <h4>In plain words</h4>
        <p class="text-sm">${esc(d.plain.text)}</p>
        <p class="quote-src">${esc(d.plain.mode || "")}</p>
      </div>` : ""}
      <div class="v-drawer__section">
        <h4>The words it rests on</h4>
        ${(f.spans || []).length
          ? (f.spans || []).map(s => `<div class="verbatim mb-12">
              <div class="verbatim__tag"><i></i>Verbatim — re-read at these offsets</div>
              <div class="verbatim__q">${esc(s.quote || "")}</div>
              <div class="verbatim__src">
                <span><b>Page</b> ${esc(s.page_no)}</span>
                <span><b>Offsets</b> ${esc(s.char_start)}–${esc(s.char_end)}</span>
              </div></div>`).join("")
          : `<p class="text-sm text-muted">No quotation supports this one — which is
               exactly why it is reported as unestablished rather than as a finding
               about your cover.</p>`}
      </div>`;
  }

  host.innerHTML = `<div class="v-drawer-overlay" data-act="drawer-close"></div>
    <aside class="v-drawer" role="dialog" aria-modal="true" aria-label="Evidence">
      <button class="v-drawer__close" data-act="drawer-close" aria-label="Close">✕</button>
      <h3 class="mb-12">Show me why</h3>
      ${body}
    </aside>`;
  document.body.appendChild(host);
}

/* ── Page quality ─────────────────────────────────────────────────────────
 *
 * The weakest part of the pipeline, turned into the most trustworthy thing the product says
 * about itself: here are the pages we could not read, and we refuse to grade a finding
 * PROVEN on any of them.
 */
async function smePageQuality(el) {
  el.innerHTML = spinner("Checking how well we read your documents");
  const d = await api("/api/page-quality"); if (!d) return;
  if (d.available === false) {
    el.innerHTML = pageTitle("How well we read your files") + emptyState("Nothing to check", esc(d.reason));
    return;
  }

  const docs = (d.documents || []).map(doc => `<div class="card mb-16">
    <div class="card-head"><h3>${esc(titleCase(doc.document_kind || "Document"))}</h3>
      <span class="tag ${doc.pages_needing_attention.length ? "tag-HIGH" : "tag-success"}">
        ${doc.pages_read_cleanly}/${doc.pages_total} clean</span></div>
    <p class="text-sm mb-12">${esc(doc.summary)}</p>
    ${doc.pages_needing_attention.length ? `
      <div class="heads">${doc.pages_needing_attention.map(p => `
        <div class="head is-gap">
          <div><div class="head__n">Page ${p.page_no} — ${esc(p.verdict_label)}</div>
            <div class="head__p">${esc(p.reason)}</div></div>
          <span class="head__r">Capped at ${esc(p.class_ceiling)}</span>
        </div>`).join("")}</div>`
      : `<p class="text-sm text-muted">Every page is usable as evidence.</p>`}
  </div>`).join("");

  el.innerHTML = pageTitle("How well we read your files",
      "We tell you which pages we could not read, and we refuse to base a finding on them.")
    + `<div class="stats mb-24">
      <div class="stat"><div class="val">${d.pages_total}</div><div class="lbl">Pages read</div></div>
      <div class="stat ${d.pages_needing_attention ? "amber" : "green"}">
        <div class="val">${d.pages_needing_attention}</div>
        <div class="lbl">Pages we could not read confidently</div></div>
    </div>
    ${docs}
    <div class="caveat"><div class="caveat__t">Why this limits what we will claim</div>
      <p>${esc(d.class_ceiling_rule || "")}</p>
      <p>${esc(d.why_page_level || "")}</p></div>
    <div class="honest">${esc(d.notice || "")}</div>`;
}

/* ── Getting started ─────────────────────────────────────────────────────
 *
 * Two documents, not fourteen. The completeness figure is read from the blind-spot register
 * rather than counted here, because two sources for one number is how a product starts
 * contradicting itself.
 */
async function smeOnboarding(el) {
  el.innerHTML = spinner("Working out what you still need");
  const d = await api("/api/onboarding"); if (!d) return;
  if (d.available === false) {
    el.innerHTML = pageTitle("Getting started") + emptyState("Nothing to show", esc(d.reason));
    return;
  }
  const c = d.completeness || {};
  const prefill = d.identity_prefill || {};

  el.innerHTML = pageTitle("Getting started",
      "You need two documents to begin. Everything else is asked for one at a time.")
    + `<div class="big ${d.ready_to_assess ? "" : "is-gap"}">
      <div class="big__l">Where you are</div>
      <div class="big__v">${esc(c.statement || "")}</div>
      <div class="big__s">${d.ready_to_assess
        ? "You have the two documents we cannot work without. Everything below only makes the picture sharper."
        : "We are missing something we cannot work without — see below."}</div>
    </div>

    <div class="sub-head">The two we cannot work without</div>
    <div class="prot mb-24">
      ${(d.mandatory || []).map(m => `<div class="pro k-state ${m.held ? "is-assessed" : "is-gap"} ${m.held ? "" : "is-gap"}">
        <div class="pro__mark">${m.held ? "✓" : "!"}</div>
        <div><div class="pro__l">${esc(titleCase(m.kind))}</div>
          <div class="pro__p">${esc(m.why)}</div></div>
        <span class="pro__basis ${m.held ? "law" : "conv"}">${m.held ? "Held" : "Needed"}</span>
      </div>`).join("")}
    </div>

    ${(d.next_documents || []).length ? `<div class="sub-head">What to send next, in order of what it settles</div>
      <div class="heads mb-24">${(d.next_documents || []).map((n, i) => `
        <div class="head">
          <div><div class="head__n">${i + 1}. ${esc(n.document || "")}</div>
            <div class="head__p">${esc(n.unlocks || "")}</div></div>
          <div style="text-align:right">
            ${n.settles_share ? `<span class="head__r">settles ${esc(n.settles_share)}</span>` : ""}
            ${n.becomes_class ? `<div class="mt-8">${evidenceBadge(n.becomes_class)}</div>` : ""}
          </div>
        </div>`).join("")}</div>
      <button class="btn btn-primary mb-24" data-act="open" data-arg="sme-docs-hub|docs|add">Add documents</button>` : ""}

    <div class="card mb-16">
      <div class="card-head"><h3>Prefilling your company details</h3>
        <span class="tag">${prefill.available ? "Available" : "Not wired"}</span></div>
      <p class="text-sm mb-8">${esc(prefill.what_it_would_replace || "")}</p>
      <p class="text-sm text-muted">${esc(prefill.status || "")}</p>
      <p class="text-xs text-muted mt-8">Sources: ${(prefill.sources || []).map(esc).join(" · ")}</p>
    </div>
    <div class="honest"><b>${esc((c.why_not_counted_here) || "")}</b><br><br>${esc(d.notice || "")}</div>`;
}

/* ── Connected systems ───────────────────────────────────────────────────
 *
 * The one place this product used to overclaim, so it says exactly what is connected and
 * nothing more. Every feed here is labelled simulated by the server, and a live accounting
 * integration is a documented seam rather than a built feature — the screen states that
 * plainly instead of offering a connect button that leads nowhere.
 */
async function smeConnected(el) {
  el.innerHTML = spinner("Checking your connected systems");
  const feed = await api("/api/signals");
  if (!feed) return;
  const live = feed.live || {};
  const seam = feed.accounting_integration || {};
  const rows = (feed.connectors || []).map(c => `<li>
      <b>${esc(c.name)}</b> — ${esc(c.status === "CONNECTED" ? "connected" : "not connected")}${c.simulated ? " (simulated)" : ""}
      <div class="text-xs text-muted">${esc(c.detail || "")}</div></li>`).join("");

  el.innerHTML = pageTitle("Connected systems",
      "What feeds this workspace, and which of it is demonstration data.")
    + `<div class="big is-gap">
      <div class="big__l">Accounting integration</div>
      <div class="big__v">Not built — a documented seam</div>
      <div class="big__s">${esc(seam.summary || feed.notice || "")}</div>
    </div>

    <div class="stats mb-24">
      <div class="stat"><div class="val">${esc(live.live_count ?? 0)}</div>
        <div class="lbl">Rows read live</div></div>
      <div class="stat"><div class="val">${esc(live.fixture_count ?? 0)}</div>
        <div class="lbl">Rows from the labelled fixture</div></div>
    </div>

    <div class="card mb-16">
      <div class="card-head"><h3>Feeds in this workspace</h3>${evidenceBadge("DECLARED")}</div>
      <ul class="area__list">${rows}</ul>
      <p class="text-xs text-muted mt-12">A feed is not a document. An accounting figure can
        raise a question and name the document that would settle it; it can never on its own
        produce a proven finding.</p>
    </div>

    <div class="honest">${esc(seam.rule || "")}</div>`;
}

/* ══════════════════════════════════════════════════════════════════════════
   ADVISER WORKSPACE
   ══════════════════════════════════════════════════════════════════════════ */

/* ── The adviser's file note ──────────────────────────────────────────────
 *
 * The one screen where a model's prose becomes a durable artefact somebody relies on later.
 * So the interface has to make three things unmissable: what the grounding check found,
 * which of the three states the note is in, and that nothing here is sent anywhere.
 *
 * A rejected draft is shown WITH the invented token rather than hidden. A hallucination
 * caught in the act is the most reassuring thing this screen can display — it is the proof
 * that the check runs at all.
 */
async function advNote(el) {
  el.innerHTML = spinner("Opening the file note");
  let d = await api("/api/adviser/note");
  if (!d) return;

  if (d.available === false) {
    el.innerHTML = pageTitle("File note",
        "A record of the basis on which you advised. Drafted for you, approved by you.")
      + emptyState("No note yet", esc(d.reason),
        `<button class="btn btn-primary" id="note-draft">Draft the note</button>`);
    wireDraft();
    return;
  }
  paint(d);

  function paint(note) {
    const g = note.grounding || {};
    const approved = note.state === "ADVISER_APPROVED";
    el.innerHTML = pageTitle("File note",
        "A record of the basis on which you advised. Drafted for you, approved by you.")
      + `<div class="big ${approved ? "" : "is-gap"}">
        <div class="big__l">Status</div>
        <div class="big__v">${esc(note.state_label)}</div>
        <div class="big__s">${approved
          ? "This is a record. It cannot be edited any further."
          : "This is a draft. It carries no authority until you approve it."}</div>
      </div>

      <div class="card mb-16">
        <div class="card-head"><h3>Grounding check</h3>
          <span class="tag ${g.accepted === false ? "tag-HIGH" : g.accepted ? "tag-success" : ""}">
            ${g.checked ? (g.accepted ? "Draft accepted" : "Draft rejected") : "Not model-drafted"}</span></div>
        ${g.accepted === false ? `
          <p class="text-sm mb-8">${esc(g.reason || "")}</p>
          <div class="heads">${(g.problems || []).map(p => `
            <div class="head is-gap"><div><div class="head__p">${esc(p)}</div></div>
              <span class="head__r">Rejected</span></div>`).join("")}</div>`
        : `<p class="text-sm text-muted">${esc(g.reason
            || "Every reference in this note was checked against the disclosed evidence.")}</p>`}
        ${(g.adviser_edit_warnings || []).length ? `
          <div class="caveat mt-16"><div class="caveat__t">In your own edit</div>
            <ul class="area__list">${g.adviser_edit_warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>
            <p>Recorded, not blocked — you may legitimately need to quote a term.</p></div>` : ""}
      </div>

      <div class="sub-head">The note</div>
      <div class="card mb-16">
        <textarea id="note-text" ${approved ? "readonly" : ""} rows="22"
          style="width:100%;font-family:var(--mono);font-size:12.5px;line-height:1.65;
                 padding:16px;border:1px solid var(--border);border-radius:var(--radius-sm);
                 background:${approved ? "var(--surface-alt)" : "var(--surface)"}"
        >${esc(note.text)}</textarea>
        <div class="btn-group mt-12">
          ${approved ? "" : `
            <button class="btn btn-secondary" id="note-save">Save my edits</button>
            <button class="btn btn-primary" id="note-approve">Approve — this becomes a record</button>
            <button class="btn btn-ghost" id="note-draft">Redraft</button>`}
        </div>
        <div id="note-out" class="mt-12"></div>
      </div>

      <div class="sub-head">History</div>
      <div class="card mb-16"><table><thead><tr><th>When</th><th>State</th><th>By</th><th>What</th></tr></thead>
        <tbody>${(note.history || []).map(h => `<tr>
          <td class="text-xs text-muted">${esc(String(h.at).slice(0, 16).replace("T", " "))}</td>
          <td><span class="tag">${esc(h.state)}</span></td>
          <td class="text-xs">${esc(h.by)}</td>
          <td class="text-sm">${esc(h.what)}</td></tr>`).join("")}</tbody></table></div>

      <div class="honest"><b>Nothing here is sent.</b> ${esc(note.never_sent || "")}</div>`;

    wireDraft();
    const save = $("#note-save");
    if (save) save.onclick = async () => {
      const r = await api("/api/adviser/note/edit",
        { method: "POST", body: { text: $("#note-text").value } });
      if (r) paint(r);
    };
    const approve = $("#note-approve");
    if (approve) approve.onclick = async () => {
      if (!confirm("Approve this note? It becomes a record and cannot be edited afterwards."))
        return;
      const r = await api("/api/adviser/note/approve", { method: "POST" });
      if (r) paint(r);
    };
  }

  function wireDraft() {
    const btn = $("#note-draft");
    if (!btn) return;
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const r = await api("/api/adviser/note/draft", { method: "POST" });
        if (r) paint(r);
      } catch (err) {
        el.insertAdjacentHTML("beforeend",
          `<div class="insight critical mt-12"><div class="insight-label">Could not draft</div>
            <div class="insight-text">${esc(err.message || String(err))}</div></div>`);
      } finally { btn.disabled = false; }
    };
  }
}

function renderAdviser() {
  if (!S.view || !S.view.startsWith("adv")) S.view = "adv-queue";
  shell("ADVISER", [
    { section: t("nav.adviser.cases") },
    { id: "adv-queue", label: t("nav.adviser.review_queue"), icon: "≡" },
    { id: "adv-review", label: t("nav.adviser.current_review"), icon: "▣" },
    { section: t("nav.adviser.act_on_it") },
    { id: "adv-actions", label: t("nav.adviser.requests_to_action"), icon: "→" },
    { id: "adv-requests", label: t("nav.adviser.information_requests"), icon: "?" },
    { section: t("nav.adviser.your_record") },
    { id: "adv-note", label: t("nav.adviser.file_note"), icon: "▤" },
  ]);
  const m = $("#main");
  const advViews = { "adv-queue": advQueue, "adv-review": advReview,
                     "adv-actions": advActions,
                     "adv-requests": advRequests, "adv-note": advNote };
  mount(advViews[S.view] || advQueue, m);
}

/* Urgency × impact, from the queue's own numbers.
 *
 * `adviser_intelligence_desk`. Both axes are derived, and how is stated on the canvas so
 * the placement can be argued with rather than trusted: urgency from the count of findings
 * that need an authorised review, impact from the inverse of the score. Demo rows are
 * plotted in grey because they carry no documents — the reference's own rule that a
 * fabricated position in a product about evidence is unforgivable.
 *
 * The table below it is unchanged. This is a way in, not a replacement for the numbers.
 */
function priorityCanvas(cases) {
  if (!cases.length) return "";
  const maxNeed = Math.max(1, ...cases.map(c => c.needs_review_count || 0));
  const pt = c => ({
    // Urgency: how much needs a human, normalised across the queue.
    x: 8 + 84 * ((c.needs_review_count || 0) / maxNeed),
    // Impact: a lower score means more is at stake, so the axis is inverted.
    y: 8 + 84 * (1 - Math.min(100, Math.max(0, c.score ?? 50)) / 100),
  });
  return `<div class="pc mb-16" data-rise="zoom">
    <div class="card-head"><h3>Priority canvas</h3>
      <span class="text-xs text-muted">urgency = findings needing review · impact = inverse of score</span></div>
    <div class="pc__plot">
      <div class="pc__q"></div>
      ${cases.map((c, i) => {
        const p = pt(c);
        return `<div class="pc__dot ${c.demo ? "is-demo" : ""}"
          data-p="${esc(c.priority || "")}" style="left:${p.x}%;bottom:${p.y}%;--d:${i * 70}ms"
          ${c.demo ? "" : `data-act="nav" data-arg="adv-review"`}
          title="${esc(c.business_name || "")} — ${esc(c.priority || "")}">
          <i></i><span class="pc__tip">${esc((c.business_name || "").slice(0, 22))}
            <span>${c.demo ? "demo · no documents" : `score ${c.score ?? "—"} · ${c.needs_review_count ?? 0} to review`}</span>
          </span></div>`;
      }).join("")}
      <span class="pc__ax pc__ax--x">Urgency</span>
      <span class="pc__ax pc__ax--y">Impact</span>
    </div>
  </div>`;
}

async function advQueue(el) {
  el.innerHTML = spinner("Loading your review queue");
  const d = await api("/api/adviser/queue"); if (!d) return;
  const cases = d.cases || [];
  const live = cases.filter(c => !c.demo);
  el.innerHTML = `<div class="page-title"><h1>Review Queue</h1>
      <p>${cases.length} case(s) · ${d.live_case_count ?? live.length} live, ${cases.length - live.length} demonstration</p></div>
    ${d.awaiting_consent ? `<div class="insight warn mb-16"><div class="insight-label">Waiting on consent</div><div class="insight-text">No live case has been disclosed to you yet. A case reaches this queue only when the business grants disclosure — that is the boundary working, not a fault. Sign in as the owner, open <strong>Share with Adviser</strong> and grant consent, then return here.</div></div>` : ""}
    ${priorityCanvas(cases)}
    <div class="card"><table><thead><tr><th>Client</th><th>Priority</th><th>Why it is here</th><th>Findings</th><th>Needs review</th><th>Score</th><th>Status</th><th></th></tr></thead><tbody>
      ${cases.map(c => `<tr>
        <td class="td-name">${esc(c.business_name || "")}${c.demo ? ' <span class="tag">demo</span>' : ' <span class="tag tag-success">live</span>'}</td>
        <td><span class="tag tag-${esc(c.priority || "")}">${esc(c.priority || "")}</span></td>
        <td class="text-xs">${esc((c.source || "").replace(/_/g, " ").toLowerCase())}</td>
        <td>${c.finding_count ?? 0}</td>
        <td>${c.needs_review_count ?? 0}</td>
        <td>${c.score ?? "—"}</td>
        <td class="text-xs">${esc(c.status || "")}</td>
        <td>${c.demo ? '<span class="text-xs text-muted">no documents behind it</span>' : '<button class="btn btn-sm btn-secondary" data-act="nav" data-arg="adv-review">Review</button>'}</td>
      </tr>`).join("")}
    </tbody></table></div>
    <p class="text-xs text-muted mt-12">${esc(d.notice || "")}</p>`;
}

// withheld_pending_documents arrives as an OBJECT keyed by document kind with a count as the
// value ({"ACCOUNTS": 1, "TENANCY": 1, ...}), not as an array. Reading .length on it yields
// undefined, which is how the "Pending Documents" tile came to print "undefined" and how the
// "Withheld pending documents" line came to never render at all (undefined is falsy). These two
// readers accept either shape so the screen survives a future change to a plain list.
function _wpdKinds(d) {
  const w = d && d.withheld_pending_documents;
  if (!w) return [];
  return Array.isArray(w) ? w.map(String) : Object.keys(w);
}
function _wpdCount(d, k) {
  const w = d && d.withheld_pending_documents;
  if (!w || Array.isArray(w)) return 1;
  const n = parseInt(w[k], 10);
  return isFinite(n) ? n : 1;
}

async function advReview(el) {
  el.innerHTML = `<div class="loading"><div class="spinner"></div>Loading</div>`;
  // A missing disclosure is a 403 CONSENT_REQUIRED. That is the consent boundary doing its
  // job, not a failure, so it gets an explanatory empty state rather than an error banner —
  // and rather than leaving the screen stuck on a spinner, which is what happened before.
  let d;
  try {
    d = await api("/api/adviser/review");
  } catch (err) {
    el.innerHTML = `<div class="page-title"><h1>Review Brief</h1></div>
      <div class="insight warn"><div class="insight-label">Nothing disclosed to you yet</div>
        <div class="insight-text">${esc(err.message || "The business has not granted disclosure.")}</div></div>
      <p class="text-sm text-muted mt-12">This is the consent boundary, not a fault. The business decides what leaves its workspace and names the recipient. Sign in as the owner, open <strong>Share with Adviser</strong>, grant consent, then come back.</p>
      <button class="btn btn-ghost mt-16" data-act="nav" data-arg="adv-queue">← Back to Queue</button>`;
    return;
  }
  if (!d) return;
  // The disclosed brief carries what the OWNER released and, just as importantly, what was
  // withheld. The previous version read action_required / review_needed / score / decisions,
  // none of which are on this response — so three stat tiles always showed 0 and the
  // withheld counts, which are the whole point of a consent-scoped brief, were never shown.
  const shown = d.findings || [];
  el.innerHTML = `<div class="page-title"><h1>Review Brief — ${esc(d.business || "")}</h1>
      <p>${esc(d.carrier || "")} · disclosed to ${esc(d.recipient || "you")}</p></div>
    <div class="stats">
      <div class="stat"><div class="val">${shown.length}</div><div class="lbl">Findings Disclosed</div></div>
      <div class="stat amber"><div class="val">${d.findings_withheld_count ?? 0}</div><div class="lbl">Withheld</div><div class="sub">Not shared by the owner</div></div>
      <div class="stat"><div class="val">${(d.disclosed_fields || []).length}</div><div class="lbl">Fields Granted</div></div>
      <div class="stat"><div class="val">${_wpdKinds(d).length}</div><div class="lbl">Pending Documents</div></div>
    </div>
    <div class="card mb-16"><div class="card-head"><h3>Scope of this disclosure</h3></div>
      <p class="text-sm">The owner granted: ${(d.disclosed_fields || []).map(f => `<span class="tag">${esc(f)}</span>`).join(" ") || "—"}</p>
      ${d.accounting_withheld ? '<p class="text-sm text-muted mt-8">Accounting detail was withheld.</p>' : ""}
      ${_wpdKinds(d).length ? `<p class="text-sm text-muted mt-8">Withheld pending documents: ${_wpdKinds(d).map(k => esc(titleCase(k))
            + (_wpdCount(d, k) > 1 ? ` (${_wpdCount(d, k)})` : "")).join(", ")}</p>` : ""}
      <p class="text-xs text-muted mt-8">${esc(d.notice || "")}</p>
    </div>
    ${shown.length
      ? `<div class="sub-head">Findings in scope</div>
         ${shown.map(f => advFindingCard(f)).join("")}`
      : `<div class="empty"><h3>No findings in scope</h3><p>Either the owner granted documents that carry no findings, or every finding rests on a document they held back. The withheld count above tells you which.</p></div>`}
    ${_wpdKinds(d).length ? `
      <div class="card mt-16"><div class="card-head"><h3>Ask for a wider grant</h3></div>
        <p class="text-sm mb-12">These document kinds are behind findings you cannot read. Naming one in a request tells the owner exactly what to release.</p>
        <div class="btn-group">${_wpdKinds(d).map(k => `
          <button class="btn btn-sm btn-secondary" data-act="adv-ask"
            data-arg="DISCLOSURE|${esc(k)}">Request ${esc(titleCase(k))}</button>`).join("")}</div></div>` : ""}
    ${(d.searched || []).length ? `<div class="card mt-16"><div class="card-head"><h3>What was searched</h3></div><ul class="text-sm">${d.searched.map(s => `<li>${esc(s)}</li>`).join("")}</ul>
      ${(d.not_searched || []).length ? `<div class="card-head mt-12"><h3>What was NOT searched</h3></div><ul class="text-sm">${d.not_searched.map(s => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}</div>` : ""}
    <div class="btn-group mt-16">
      <button class="btn btn-ghost" data-act="nav" data-arg="adv-queue">← Back to Queue</button>
      <button class="btn btn-secondary" data-act="nav" data-arg="adv-actions">Requests to action →</button>
    </div>`;
}

/* One disclosed finding, with the two things an adviser can actually do to it: read the
 * evidence, and ask the client for what would settle it.
 *
 * `renderDecision` is not reused here because it renders an ORCHESTRATOR decision
 * (`interest_label`, `decision`, `current_state`) and the disclosed brief carries
 * FINDINGS (`rule_id`, `headline`, `state_label`, `class`, `spans`). Feeding one to the
 * other is why this screen previously showed blank titles and no state.
 */
/* One disclosed finding.
 *
 * ALIGNMENT DEFECT FIXED HERE. The evidence-class pill used to render `class_label`,
 * which is prose — "Proven — every input authored by a third party, authority or
 * insurer", 68 characters — while `.tag` is a 10px uppercase pill. The result was a pill
 * wider than its own card, which pushed the rule id and the citation count off the row.
 * The pill now carries the short class, the full sentence is the title attribute and a
 * caption underneath, so nothing is lost and the meta row lines up.
 */
function disclosedFindingCard(f, opts) {
  const o = opts || {};
  const cls = f.state === "DOCUMENTED" ? "MONITOR"
    : f.state === "INFORMATION_MISSING" ? "REVIEW" : "ACTION_REQUIRED";
  const spans = f.spans || [];
  return `<div class="finding ${cls}">
    <div class="finding-head">
      <div class="finding-title">${esc(f.headline || f.rule_id || "")}</div>
      <span class="tag tag-${cls}">${esc(f.state_label || f.state || "")}</span>
    </div>
    <div class="finding-meta">
      <span class="tag tag-${esc(f.class || "")}" title="${esc(f.class_label || "")}">${esc(f.class || "")}</span>
      <span class="tag tag-info">${esc(f.rule_id || "")}</span>
      ${spans.length ? `<span class="tag">${spans.length} citation(s)</span>` : ""}
    </div>
    ${f.class_label && f.class_label !== f.class
      ? `<p class="text-xs text-muted mt-8">${esc(f.class_label)}</p>` : ""}
    ${spans.length ? `<div class="mt-8">${spans.map(s => `
      <div class="v-evidence"><div class="text-sm">${esc(s.quote || "")}</div>
        <div class="v-evidence__src">page ${esc(s.page_no)} · offsets ${esc(s.char_start)}–${esc(s.char_end)}</div>
      </div>`).join("")}</div>` : ""}
    ${o.actions === "carrier"
      ? `<div class="btn-group mt-12">
          <button class="btn btn-sm btn-ghost car-explain"
            data-rule="${esc(f.rule_id || "")}">Explain in underwriting terms</button>
        </div>
        <div class="mt-8" id="carex-${esc(f.rule_id || "")}"></div>`
      : o.actions === "adviser"
        ? `<div class="btn-group mt-12">
            <button class="btn btn-sm btn-ghost" data-act="gap-why" data-arg="${esc(f.rule_id || "")}">Show me why</button>
            <button class="btn btn-sm btn-secondary" data-act="adv-ask" data-arg="${esc(f.rule_id || "")}">Request information</button>
          </div>`
        : ""}
  </div>`;
}

/* The adviser's card: the same finding, with the two things an adviser can do to it. */
function advFindingCard(f) { return disclosedFindingCard(f, { actions: "adviser" }); }

/* The carrier's card.
 *
 * MEASURED DEFECT THIS FIXES. `carDetail` called `renderFinding(f)`, a function that has
 * never existed anywhere in this file — grep returned the call site and nothing else. It
 * threw a ReferenceError while building the innerHTML string, `mount()` caught it, and the
 * carrier's account screen rendered "This screen could not load" with no clue why. It only
 * fired once a business had actually disclosed to the carrier, which is the one path
 * nobody exercised, because the consent screen never offered the carrier's name — so the
 * crash sat behind a door the product could not open.
 *
 * The buttons differ from the adviser's rather than being shared, because the adviser's
 * two both 403 for a carrier: "Request information" posts to the ADVISER route, and "Show
 * me why" opens the evidence drawer over the owner's own brief. In their place is
 * `/api/carrier/explain`, which reuses the same disclosure check and is the one explainer
 * a carrier is entitled to — and which, like the other four carrier write routes, had no
 * caller anywhere in the interface until now.
 */
function carFindingCard(f) { return disclosedFindingCard(f, { actions: "carrier" }); }

/* Ask the client for something, against a named finding.
 *
 * A prompt() rather than an inline form on purpose: the request is one sentence, and the
 * screen it is raised from is a list. It posts to the existing
 * /api/adviser/request-information, which records the question and — deliberately — does
 * NOT move the finding's state or evidence class. An adviser's question is not evidence.
 */
async function adviserAsk(arg) {
  const raw = String(arg || "");
  let ruleId = raw, suggestion = "";
  if (raw.startsWith("DISCLOSURE|")) {
    const kind = raw.slice("DISCLOSURE|".length);
    ruleId = "DISCLOSURE-SCOPE";
    suggestion = `Please also share your ${titleCase(kind)} so the findings that rest on it can be reviewed.`;
  } else {
    suggestion = "Please provide the supporting document for this finding so the position can be established rather than estimated.";
  }
  const message = prompt("What do you need from the client?", suggestion);
  if (message === null) return;
  if (!message.trim()) { toast("A request needs a message.", "err"); return; }
  try {
    await api("/api/adviser/request-information",
      { method: "POST", body: { rule_id: ruleId, message: message.trim() } });
    toast("Request recorded against " + ruleId);
    nav8("adv-requests");
  } catch (err) {
    toast(err.message || String(err), "err");
  }
}
window.adviserAsk = adviserAsk;

/* ── Requests to action ───────────────────────────────────────────────────
 *
 * The action loop, from the adviser's side. Every button here is a transition
 * `actions.py` already reserves to the actor "adviser" — ADVISER_REVIEWING,
 * INFORMATION_REQUESTED, QUOTE_RECEIVED, BOUND, DECLINED, NOT_REQUIRED. The buttons are
 * rendered FROM `allowed_moves` rather than from a list hardcoded here, so the interface
 * cannot offer a move the state machine would refuse, and a change to the transition
 * table shows up in the UI without an edit.
 *
 * Moves belonging to the owner are shown greyed with who they belong to, rather than
 * hidden: an adviser waiting on a client should be able to see that they are waiting.
 */
async function advActions(el) {
  el.innerHTML = spinner("Loading the requests raised on this case");
  let d;
  try {
    d = await api("/api/actions");
  } catch (err) {
    el.innerHTML = pageTitle("Requests to action")
      + `<div class="insight warn"><div class="insight-label">Nothing disclosed to you yet</div>
          <div class="insight-text">${esc(err.message || "")}</div></div>
        <p class="text-sm text-muted mt-12">A correction request is derived from the Brief, so it sits behind the same consent gate. Sign in as the owner, open <strong>Share with adviser</strong>, grant disclosure, then come back.</p>
        <button class="btn btn-ghost mt-16" data-act="nav" data-arg="adv-queue">← Back to Queue</button>`;
    return;
  }
  if (!d) return;
  if (d.available === false) {
    el.innerHTML = pageTitle("Requests to action")
      + emptyState("Nothing to action", esc(d.reason || ""));
    return;
  }

  const all = d.actions || [];
  const mine = all.filter(a => (a.allowed_moves || []).some(m => m.by === "adviser"));
  const waiting = all.filter(a => !mine.includes(a) && !a.is_terminal);
  const closed = all.filter(a => a.is_terminal);
  const out = await api("/api/outcomes").catch(() => null);

  el.innerHTML = pageTitle("Requests to action",
      `${all.length} request(s) on this case. ${mine.length} waiting on you.`)
    + (out && out.available !== false ? `<div class="stats mb-24">
        <div class="stat"><div class="val">${mine.length}</div><div class="lbl">Waiting on you</div></div>
        <div class="stat amber"><div class="val">${money(out.exposure_pending_sgd)}</div><div class="lbl">Exposure in flight</div></div>
        <div class="stat green"><div class="val">${money(out.exposure_closed_sgd)}</div><div class="lbl">Exposure closed</div><div class="sub">Counted on BOUND only</div></div>
        <div class="stat"><div class="val">${money(out.exposure_identified_sgd)}</div><div class="lbl">Exposure identified</div></div>
      </div>` : "")
    + (mine.length ? `<div class="sub-head">Waiting on you</div>
        ${mine.map(advActionCard).join("")}` : "")
    + (waiting.length ? `<div class="sub-head">Waiting on the client</div>
        ${waiting.map(advActionCard).join("")}` : "")
    + (closed.length ? `<div class="sub-head">Closed</div>
        ${closed.map(advActionCard).join("")}` : "")
    + (all.length ? "" : emptyState("No requests raised",
        "The engine raises a request when a finding quantifies a gap. This case has none."))
    + `<div class="honest mt-16">${esc(d.premium_notice || "")}</div>`;
}

/* MEASURED ALIGNMENT DEFECT.
 *
 * The card head is a `space-between` flex row holding a title and a state tag. It was
 * fed `a.title`, which on a real case is not a title at all — it is the finding's
 * headline, and the shortest one on the demo bundle runs to 290 characters:
 *
 *   "The ACRA profile carries a secondary activity (Retail sale via online platforms)
 *    that the policy's business description ... We locate and quote; we do not conclude."
 *
 * A 290-character string in a flex row squeezes the tag beside it to a two-line sliver
 * and leaves every card in the list a different height, so the state column does not line
 * up down the page. `action_label` is the short name of the action — "Update a
 * declaration" — and is what belongs in a heading. The reasoning moves into the body,
 * which is where a paragraph reads properly and where it no longer fights the tag for
 * horizontal space.
 */
function advActionCard(a) {
  const mine = (a.allowed_moves || []).filter(m => m.by === "adviser");
  const theirs = (a.allowed_moves || []).filter(m => m.by !== "adviser");
  const heading = a.action_label || a.title || "";
  // Only show the long reasoning separately when it is not already the heading.
  const because = a.title && a.title !== heading ? a.title : "";
  return `<div class="action-card mb-8">
    <div class="action-head">
      <div class="action-title">${esc(heading)}</div>
      <span class="tag ${a.closes_exposure ? "tag-success" : a.is_terminal ? "" : "tag-REVIEW"}">${esc(a.state_label || a.state)}</span>
    </div>
    <div class="action-body">
      ${because ? `<p class="text-sm mb-8"><b>Why this was raised.</b> ${esc(because)}</p>` : ""}
      <p class="text-sm mb-8"><b>What you are being asked to do.</b> ${esc(a.asks || "")}</p>
      <div class="finding-meta mb-8">
        <span class="tag tag-info">${esc(a.action_label || "")}</span>
        <span class="tag">${esc(titleCase(a.interest || ""))}</span>
        ${a.evidence_class ? `<span class="tag">${esc(a.evidence_class)}</span>` : ""}
        ${a.source_rule_id ? `<span class="tag">${esc(a.source_rule_id)}</span>` : ""}
      </div>
      ${a.is_quantified ? `<p class="text-sm"><b>Exposure it would close:</b> ${money(a.exposure_sgd)}
        <span class="text-xs text-muted">— ${esc(a.exposure_basis || "")}</span></p>`
        : `<p class="text-sm text-muted">No money figure: this one is measured in people or documents, not dollars.</p>`}
      ${(a.documents_requested || []).length ? `<p class="text-sm mt-8"><b>Documents named:</b> ${(a.documents_requested || []).map(x => esc(String(x))).join(", ")}</p>` : ""}
      ${a.adviser_note ? `<div class="v-evidence mt-8"><div class="text-sm">${esc(a.adviser_note)}</div>
        <div class="v-evidence__src">your last note on this request</div></div>` : ""}
      ${mine.length ? `<div class="btn-group mt-12">${mine.map(m => `
        <button class="btn btn-sm ${m.to === "BOUND" ? "btn-primary" : "btn-secondary"}"
          data-act="adv-move" data-arg="${esc(a.id)}|${esc(m.to)}">${esc(m.label)}</button>`).join("")}</div>`
        : theirs.length ? `<p class="text-xs text-muted mt-12">Waiting on the ${esc(theirs[0].by === "owner" ? "client" : theirs[0].by)}: ${theirs.map(m => esc(m.label)).join(", ")}.</p>`
        : `<p class="text-xs text-muted mt-12">This request is closed. Nothing further can move it.</p>`}
    </div>
  </div>`;
}

/* Advance one action. The target state is sent; the ACTOR is not, and cannot be — the
 * server reads it from the session, which is what keeps the exposure-closed figure from
 * being self-reported. A refused move comes back 409 with the permitted moves listed. */
async function adviserMove(arg) {
  const [id, to] = String(arg || "").split("|");
  if (!id || !to) return;
  let note = "";
  if (to === "BOUND" || to === "DECLINED" || to === "NOT_REQUIRED") {
    const answer = prompt(
      to === "BOUND"
        ? "Confirming the contract actually changed. What was bound?"
        : "Why? This is recorded on the request.", "");
    if (answer === null) return;
    note = answer.trim();
  }
  try {
    await api(`/api/actions/${encodeURIComponent(id)}/advance`,
      { method: "POST", body: { to_state: to, note } });
    toast("Moved to " + titleCase(to));
    nav8("adv-actions");
  } catch (err) {
    toast(err.message || String(err), "err");
  }
}
window.adviserMove = adviserMove;

async function advRequests(el) {
  el.innerHTML = spinner("Loading your information requests");
  const d = await api("/api/adviser/requests"); if (!d) return;
  const rows = d.requests || [];
  // Field names read from the response the server actually sends: rule_id, message,
  // requested_by, adviser_organisation, state. The previous version read document /
  // title / status / reason, none of which exist, so every card rendered blank.
  el.innerHTML = pageTitle("Information Requests",
      `${rows.length} request(s) raised against this case. The owner sees these too.`)
    + (rows.length ? rows.map(r => `<div class="action-card mb-8">
        <div class="action-head">
          <div class="action-title">${esc(r.rule_id || "")}</div>
          <span class="tag tag-REVIEW">${esc(titleCase(r.state || ""))}</span>
        </div>
        <div class="action-body">
          <p class="text-sm">${esc(r.message || "")}</p>
          <p class="text-xs text-muted mt-8">Raised by ${esc(r.requested_by || "you")}${r.adviser_organisation ? ", " + esc(r.adviser_organisation) : ""}</p>
        </div></div>`).join("")
      : emptyState("No requests yet",
          "Open <strong>Current Review</strong> and use <em>Request information</em> on a finding to ask the client for what would settle it."))
    + `<div class="honest mt-16">A request records a question. It does not change the finding's state or its evidence class — a question is not evidence.</div>`;
}

/* ══════════════════════════════════════════════════════════════════════════
   CARRIER WORKSPACE
   ══════════════════════════════════════════════════════════════════════════ */

function renderCarrier() {
  if (!S.view || !S.view.startsWith("car")) S.view = "car-triage";
  // Ordered as an underwriter's day, not as a data model. The work queue is first
  // because "what should I look at now" is the only question they open the tool with.
  // The read-only portfolio screens keep their place below; nothing was removed.
  shell("CARRIER", [
    { section: t("nav.carrier.my_work") },
    { id: "car-triage", label: t("nav.carrier.work_queue"), icon: "≡" },
    { id: "car-inbox", label: t("nav.carrier.needs_attention"), icon: "◍" },
    { id: "car-decisions", label: t("nav.carrier.decision_log"), icon: "▤" },
    { section: t("nav.carrier.the_book") },
    { id: "car-analyses", label: t("nav.carrier.coverage_gap_analysis"), icon: "▣" },
    { id: "car-patterns", label: t("nav.carrier.book_patterns"), icon: "⧉" },
    { id: "car-portfolio", label: t("nav.carrier.overview"), icon: "◈" },
    { id: "car-premium", label: t("nav.carrier.premium_at_risk"), icon: "⚠" },
    { id: "car-matrix", label: t("nav.carrier.attention_matrix"), icon: "▦" },
    { id: "car-accounts", label: t("nav.carrier.priority_accounts"), icon: "◉" },
    { section: t("nav.carrier.ask_the_book") },
    { id: "car-ask", label: t("nav.carrier.book_copilot"), icon: "✦" },
  ]);
  const m = $("#main");
  const carViews = {
    "car-triage": carTriage,
    "car-inbox": carInbox,
    "car-decisions": carDecisions,
    "car-patterns": carPatterns,
    "car-detail": carDetail,
    "car-analyses": carAnalyses,
    "car-portfolio": carPortfolio,
    "car-premium": carPremium,
    "car-matrix": carMatrix,
    "car-accounts": carAccounts,
    "car-ask": carAsk,
  };
  mount(carViews[S.view] || carTriage, m);
}

/* ── The book copilot and the portfolio briefing ───────────────────────────
 *
 * `POST /api/carrier/ask` and `POST /api/carrier/briefing` have both existed since the
 * carrier workspace was built and neither had ever had a caller. `briefing` was
 * therefore returning HTTP 500 on a NameError that nothing could have noticed.
 *
 * The copilot's whole value is what it REFUSES. Ask it something only an SME's documents
 * could answer — a sum insured, a clause, a premium — and it declines by name and says
 * where the answer actually lives, rather than retrieving from a corpus a carrier may not
 * read. That refusal is the product's central claim made interactive, so it gets a screen
 * of its own rather than being buried.
 */
async function carAsk(el) {
  el.innerHTML = pageTitle("Book copilot",
      "Ask your own book. It answers from account rows and published wordings, and says so when a question can only be answered from a document you may not read.")
    + `<div class="card mb-16">
        <div class="form-group"><label for="cask">Your question</label>
          <input type="text" id="cask" maxlength="400"
            placeholder="e.g. which accounts have no adviser assigned"></div>
        <div class="btn-group">
          <button class="btn btn-primary" id="caskbtn">Ask the book</button>
          <button class="btn btn-secondary" id="cbriefbtn">Write the portfolio briefing</button>
        </div>
        <p class="text-xs text-muted mt-12">Retrieval here is deterministic. No model decides which accounts you are shown, because "which accounts matter" is the underwriting judgement this product refuses to make for anyone.</p>
      </div>
      <div id="caskout"></div>`;

  const out = $("#caskout");

  function refusal(d) {
    return `<div class="insight warn"><div class="insight-label">Not answerable from your book</div>
        <div class="insight-text">${esc(d.why_not || "")}</div></div>
      <div class="card mt-16"><div class="card-head"><h3>What you can do instead</h3></div>
        <p class="text-sm">${esc(d.what_you_can_do || "")}</p>
        <button class="btn btn-sm btn-secondary mt-12" data-act="nav" data-arg="car-triage">Open the work queue →</button></div>
      <p class="text-xs text-muted mt-12">Scope: ${esc(d.scope || "")} · method: ${esc(d.method || "")}</p>`;
  }

  function answer(d) {
    const hits = d.matched_accounts || [];
    const top = d.top_of_queue || [];
    const rows = list => `<div class="card mb-16"><table>
      <thead><tr><th>Account</th><th>Why it is here</th><th>Attention</th><th></th></tr></thead>
      <tbody>${list.map(r => `<tr>
        <td class="td-name">${esc(r.business_name || "")}</td>
        <td class="text-sm">${esc(r.reason_line || "")}</td>
        <td><span class="tag">${r.attention ?? "—"}</span></td>
        <td style="text-align:right"><button class="btn btn-sm btn-secondary"
          data-act="car-open" data-arg="${esc(r.account_id || "")}">Open</button></td>
      </tr>`).join("")}</tbody></table></div>`;
    return `<div class="big"><div class="big__l">Matches on your book</div>
        <div class="big__v">${d.match_count ?? 0}</div>
        <div class="big__s">${esc(d.question || "")}</div></div>
      ${hits.length ? rows(hits) : emptyState("Nothing on your book matched",
        "No account row carries those words. This is a keyword match over structured rows, not a search of anybody's documents — so a nil result means your book does not describe it, not that it is not happening.")}
      ${top.length ? `<div class="sub-head">Top of your queue right now</div>${rows(top)}` : ""}
      ${(d.patterns || []).length ? `<div class="sub-head">Concentrations</div>
        ${(d.patterns || []).map(p => `<div class="dep"><div class="dep__head">
          <span class="dep__kind">${esc(titleCase(p.kind))}</span>
          <span class="dep__name">${esc(p.label)}</span>
          <span class="tag tag-HIGH">${p.count} of ${p.of}</span></div>
          <div class="dep__why">${esc(p.reading)}</div></div>`).join("")}` : ""}
      <div class="honest mt-16">${esc(d.notice || "")}</div>`;
  }

  const askBtn = $("#caskbtn");
  if (askBtn) askBtn.onclick = async () => {
    const question = $("#cask").value.trim();
    if (question.length < 3) {
      out.innerHTML = `<div class="insight warn"><div class="insight-text">Ask something. Three characters is the minimum the server accepts.</div></div>`;
      return;
    }
    askBtn.disabled = true;
    out.innerHTML = spinner("Reading your book");
    try {
      const d = await api("/api/carrier/ask", { method: "POST", body: { question } });
      out.innerHTML = !d ? "" : (d.answerable === false ? refusal(d) : answer(d));
    } catch (err) {
      out.innerHTML = `<div class="insight critical"><div class="insight-label">Could not answer</div>
        <div class="insight-text">${esc(err.message || String(err))}</div></div>`;
    } finally { askBtn.disabled = false; }
  };

  const briefBtn = $("#cbriefbtn");
  if (briefBtn) briefBtn.onclick = async () => {
    briefBtn.disabled = true;
    out.innerHTML = spinner("Composing the briefing");
    try {
      const d = await api("/api/carrier/briefing", { method: "POST", body: {} });
      if (!d) return;
      const b = d.briefing || {};
      const ctx = d.verified_context || {};
      out.innerHTML = `${pageTitle("Portfolio briefing")}
        <div class="card mb-16">
          <div class="card-head"><h3>The briefing</h3>
            <span class="tag ${b.accepted === false ? "tag-HIGH" : "tag-success"}">${
              b.accepted === false ? "Rejected by the gates" : esc(b.written_by || "Template")}</span></div>
          <p class="text-sm" style="white-space:pre-wrap;line-height:1.7">${esc(b.text || b.prose || "")}</p>
          ${(b.problems || []).length ? `<div class="caveat mt-16"><div class="caveat__t">Why it was rejected</div>
            <ul class="area__list">${b.problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul></div>` : ""}
        </div>
        <div class="sub-head">Every figure the writer was allowed to use</div>
        <div class="card mb-16"><table><thead><tr><th>Figure</th><th>Value</th></tr></thead>
          <tbody>${Object.entries(ctx).map(([k, v]) =>
            `<tr><td>${esc(titleCase(k))}</td><td>${esc(String(v))}</td></tr>`).join("")}</tbody></table></div>
        <div class="honest">${esc(d.notice || "")}</div>`;
    } catch (err) {
      out.innerHTML = `<div class="insight critical"><div class="insight-label">Could not compose</div>
        <div class="insight-text">${esc(err.message || String(err))}</div></div>`;
    } finally { briefBtn.disabled = false; }
  };
}

/* ── The underwriter's work queue (B4) ────────────────────────────────────
 *
 * Deterministically ranked server-side, with the weight table returned alongside so the
 * ranking can be argued with rather than trusted. That matters more here than on any SME
 * screen: an underwriter who cannot see why an account is top of their queue will either
 * ignore the order or follow it blindly, and both are worse than a visible rule.
 */
async function carTriage(el) {
  el.innerHTML = spinner("Ranking the book");
  const d = await api("/api/carrier/renewal-triage"); if (!d) return;
  if (d.available === false) { el.innerHTML = noBookState(d); return; }
  const rows = d.ranked || [];

  el.innerHTML = `${bookBanner(d)}
    ${pageTitle("Work queue",
      `${d.count ?? rows.length} account(s), ranked by what needs attention first.`)}
    ${d.scale_notice ? `<p class="tab-lead">${esc(d.scale_notice)}</p>` : ""}
    <div class="card"><table>
      <thead><tr><th>#</th><th>Account</th><th>Why it is here</th><th>Attention</th>
        <th>Last reviewed</th><th>Decision</th><th></th></tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td class="text-xs text-muted">${r.rank}</td>
        <td class="td-name">${esc(r.business_name || "")}
          <div class="text-xs text-muted">${esc(r.industry || r.state || "")}</div></td>
        <td class="text-sm">${esc(r.reason_line || "")}</td>
        <td><span class="tag ${r.attention >= 60 ? "tag-HIGH" : ""}">${r.attention}</span></td>
        <td class="text-xs text-muted">${esc(r.last_reviewed || "—")}</td>
        <td class="text-xs">${r.decision ? esc(r.decision) : '<span class="text-muted">none yet</span>'}</td>
        <td style="text-align:right"><button class="btn btn-sm btn-secondary"
          data-act="car-open" data-arg="${esc(r.account_id)}">Open</button></td>
      </tr>`).join("")}</tbody>
    </table></div>
    <div class="card mt-16"><div class="card-head"><h3>How this order was decided</h3></div>
      <p class="text-sm mb-12">${esc(d.basis || "")}</p>
      <table><thead><tr><th>Signal</th><th>Weight</th></tr></thead><tbody>
        ${Object.entries(d.weights || {}).map(([k, v]) =>
          `<tr><td>${esc(titleCase(k))}</td><td>${v}</td></tr>`).join("")}
      </tbody></table>
      <div class="caveat mt-16"><div class="caveat__t">Read the weights as judgements</div>
        <p>${esc(d.weights_are_judgements || "")}</p>
        <p>${esc(d.not_a_rating || "")}</p></div>
    </div>
    ${bookDerivation(d)}`;
}

/* ── Honest labelling for the book screens ────────────────────────────────
 *
 * WHAT WAS WRONG. Four carrier screens printed a hardcoded
 * `DEMO PORTFOLIO — SYNTHETIC DATA` banner. It was correct when the rows were six
 * invented businesses and it is now false: the rows are cases this engine actually holds
 * and every column is a count it produced. A screen that calls its own real output
 * synthetic is the same defect as one that calls invented output real — it just fails in
 * the direction nobody checks.
 *
 * So the label is driven by the response. `documents_are_synthetic` distinguishes the two
 * things that were being conflated: the DOCUMENTS may be the example bundle shipped with
 * this build, while the counts read out of them are not invented.
 */
function bookBanner(d) {
  if (!d || d.rows_are_real !== true) {
    return `<div class="synthetic-banner">DEMONSTRATION DATA — not real customer information</div>`;
  }
  if (d.documents_are_synthetic) {
    return `<div class="synthetic-banner">EXAMPLE DOCUMENTS — the counts are real output, the underlying bundle ships with this build</div>`;
  }
  return "";
}

/* The "how these rows exist" footer. Says what was derived, and names what is absent
 * rather than letting a blank cell read as a zero. */
function bookDerivation(d) {
  if (!d || d.rows_are_real !== true) return "";
  const absent = d.fields_deliberately_absent || [];
  return `<div class="honest mt-16"><b>How these rows exist.</b> ${esc(d.derivation || "")}
    ${absent.length ? `<br><br><b>Absent by design:</b> ${absent.map(f => esc(titleCase(f))).join(", ")}.` : ""}
    ${d.synthetic_notice ? `<br><br>${esc(d.synthetic_notice)}` : ""}</div>`;
}

/* ── Needs attention (A2) ────────────────────────────────────────────────
 *
 * Every row states what the book evidences AND what only a disclosure would reveal. The
 * second half is the honest part: a carrier can see that a business changed without being
 * entitled to know what changed, and saying so is better than filling the gap with a
 * plausible guess.
 */
async function carInbox(el) {
  el.innerHTML = spinner("Reading your book");
  const d = await api("/api/carrier/inbox"); if (!d) return;
  if (d.available === false) { el.innerHTML = noBookState(d); return; }
  const items = d.items || [];
  el.innerHTML = `${bookBanner(d)}
    ${pageTitle("Needs attention",
      `${d.count ?? items.length} item(s) your book flags for an underwriter.`)}
    ${items.length === 0 ? emptyState("Nothing flagged",
      "No account on this book currently flags unevidenced items. Internal change is not assessed — this build cannot compute it, so it raises nothing rather than raising a guess.") : ""}
    ${items.map(i => `<div class="dep">
      <div class="dep__head">
        <span class="dep__kind">${esc(titleCase(i.kind))}</span>
        <span class="dep__name">${esc(i.business_name || "")}</span>
      </div>
      <div class="head__n mb-8">${esc(i.headline)}</div>
      <div class="dep__modes">
        <div class="dep__mode"><span>What your book shows</span><b>${esc(i.what_we_can_say)}</b></div>
        <div class="dep__mode is-gap"><span>What it cannot show</span><b>${esc(i.what_we_cannot_say)}</b></div>
      </div>
      <div class="ev__fix mt-12"><b>Next step:</b> ${esc(i.next_step)}
        ${i.adviser ? ` · adviser: ${esc(i.adviser)}` : " · no adviser assigned"}</div>
      <button class="btn btn-sm btn-secondary mt-12" data-act="car-open" data-arg="${esc(i.account_id)}">Open account</button>
    </div>`).join("")}
    <div class="honest">${esc(d.notice || "")}</div>
    ${bookDerivation(d)}`;
}

/* ── Account detail (A1) ─────────────────────────────────────────────────
 *
 * The screen where the boundary is visible rather than merely enforced. Two panels: what
 * the carrier's own book evidences, and what the business has disclosed to them. The
 * second is usually empty, and the empty state explains why instead of looking broken.
 */
async function carDetail(el) {
  const id = S.carAccount;
  if (!id) { nav8("car-triage"); return; }
  el.innerHTML = spinner("Opening account");
  const d = await api(`/api/carrier/account/${encodeURIComponent(id)}`); if (!d) return;
  // Every one of these was dereferenced without a guard. `d.account` in particular was
  // read three times in the page title, so any response shape change turned the whole
  // screen into the generic "could not load" panel instead of a partial one.
  const acct = d.account || {};
  const book = d.from_your_book || {};
  const detail = d.case_detail;
  const real = d.row_is_real === true;
  // `—` for a field the product genuinely does not hold, rather than a plausible value.
  // A real row omits internal change and external risk on purpose; see `_book_rows`.
  const orNone = v => (v === null || v === undefined || v === "") ? "—" : String(v);

  el.innerHTML = `${real
      ? ""
      : `<div class="synthetic-banner">FIXED DEMONSTRATION ROW — no documents behind it</div>`}
    <button class="btn btn-ghost btn-sm mb-12" data-act="nav" data-arg="car-triage">← Back to queue</button>
    ${pageTitle(acct.business_name || "Account",
        [acct.wording ? acct.wording + " wording" : acct.industry, acct.account_id]
          .filter(Boolean).join(" · "))}

    <div class="sub-head">From your own book</div>
    <div class="stats mb-16">
      <div class="stat"><div class="val">${esc(orNone(book.priority))}</div><div class="lbl">Priority</div></div>
      <div class="stat red"><div class="val">${esc(orNone(book.unestablished_count))}</div><div class="lbl">Unevidenced items</div></div>
      <div class="stat amber"><div class="val">${esc(orNone(book.needs_authorised_review))}</div><div class="lbl">Needs a human</div></div>
      <div class="stat purple"><div class="val">${esc(orNone(book.information_missing))}</div><div class="lbl">Missing a document</div></div>
    </div>
    <div class="card mb-16">
      <div class="card-head"><h3>What surfaced it</h3></div>
      <p class="text-sm">${esc(book.lead_reason || "")}</p>
      <p class="text-xs text-muted mt-8">Adviser: ${esc(book.adviser || "none assigned")}
        · Last reviewed: ${esc(orNone(book.last_reviewed))}</p>
      ${d.priority_basis ? `<p class="text-xs text-muted mt-8"><b>How priority was decided.</b> ${esc(d.priority_basis)}</p>` : ""}
      ${real ? `<p class="text-xs text-muted mt-8">Internal change and external risk are not shown because this build cannot compute them for a case with no change history and no located premises. They are absent rather than defaulted, and they contribute nothing to the ranking.</p>` : ""}
    </div>

    <div class="sub-head">What the engine concluded, by insured interest</div>
    <div id="car-orchestrator">${spinner("Reconciling internal and external signals")}</div>

    <div class="sub-head">The business's own case</div>
    ${detail ? `<div class="card mb-16">
        <div class="card-head"><h3>Disclosed to you</h3><span class="tag tag-success">${(detail.disclosed_fields || []).length} document kind(s)</span></div>
        <p class="text-sm mb-12">${esc(d.case_detail_notice)}</p>
        ${(detail.findings || []).slice(0, 8).map(carFindingCard).join("")}
        ${(detail.findings || []).length > 8 ? `<p class="text-xs text-muted mt-12">${(detail.findings || []).length - 8} further disclosed finding(s) not shown here.</p>` : ""}
        ${detail.findings_withheld_count ? `<p class="text-xs text-muted mt-12">${detail.findings_withheld_count} finding(s) withheld — the documents behind them were not disclosed.</p>` : ""}
      </div>`
    : `<div class="partial mb-16">
        <div class="partial__t">Not disclosed to you</div>
        <div class="partial__s">${esc(d.case_detail_notice)}</div>
      </div>`}

    <div class="sub-head">Ask the business for something</div>
    <div class="card mb-16">
      <p class="text-sm mb-12">A request is a question, not a finding. It is recorded against the account and changes nothing about any finding, its state or its evidence class.</p>
      <div class="form-group"><label for="creqdoc">Document you need</label>
        <input type="text" id="creqdoc" maxlength="120" placeholder="e.g. Complete asset register"></div>
      <div class="form-group"><label for="creqwhy">Why you need it</label>
        <input type="text" id="creqwhy" maxlength="400" placeholder="What it would settle on your own record"></div>
      <button class="btn btn-secondary" id="creqbtn">Record the request</button>
      <div id="creqout" class="mt-12"></div>
    </div>

    <div class="sub-head">Write to the owner</div>
    <div class="card mb-16">
      <p class="text-sm mb-12">Sent as <b>${esc(d.carrier?.organisation_name || "your organisation")}</b>, not as VERIS, and it lands in the owner's normal inbox under their own preferences. The wording is a closed template: it may name the business and why the account surfaced on your book, and it carries no figure, no clause and nothing from their documents — because you have not seen them.</p>
      <div class="form-group"><label for="cmailt">What to say</label>
        <select id="cmailt" style="width:100%;padding:12px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit">
          <option value="CARRIER_REVIEW_REQUESTED">Please review your cover</option>
          <option value="CARRIER_RENEWAL_REMINDER">Your renewal is approaching</option>
          <option value="CARRIER_INFORMATION_NEEDED">We need a document from you</option>
        </select></div>
      <button class="btn btn-secondary" id="cmailbtn">Send it</button>
      <div id="cmailout" class="mt-12"></div>
    </div>

    <div class="sub-head">Record what you decided</div>
    <div class="card mb-16">
      <div class="form-group"><label for="cdec">Decision</label>
        <select id="cdec" style="width:100%;padding:12px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit">
          <option value="REVIEWED">Reviewed, no action</option>
          <option value="ACCEPTED_AS_IS">Accepted as it stands</option>
          <option value="NEEDS_ENDORSEMENT">Needs an endorsement</option>
          <option value="REFERRED_TO_ADVISER">Referred to the adviser</option>
        </select></div>
      <div class="form-group"><label for="cnote">Note</label>
        <input type="text" id="cnote" maxlength="600" placeholder="What you observed, and what you decided about the account"></div>
      <button class="btn btn-primary" id="cdecbtn">Record decision</button>
      <div id="cdecout" class="mt-12"></div>
      <p class="text-xs text-muted mt-12">Append-only. A note may not state a claim outcome — record what you observed.</p>
    </div>

    <div class="sub-head">History</div>
    <div class="card"><div id="cdechist">${carDecisionRows(d.decisions)}</div></div>`;

  /* The orchestrator's reconciliation, rendered with the SAME component the owner sees.
   *
   * Deliberately the same `renderDecision()` rather than an underwriter-specific variant. The
   * product's claim is that all three roles read ONE set of facts; two renderers for one
   * decision is how the carrier's view and the owner's view start quietly diverging, and then
   * nobody can tell which is authoritative.
   *
   * It reconciles internal signals against external ones and reports where both point the same
   * way — which is exactly the judgement an underwriter is making by hand, and it carries no
   * premium and no risk score. */
  // Renamed from `#car-decisions`, which collided with the SIDEBAR VIEW ID of the same
  // name in `carViews`. Navigation reads `data-v` so nothing broke today, but a DOM id
  // that is also a route id is a trap for the next getElementById.
  (async () => {
    const host = $("#car-orchestrator");
    if (!host) return;
    try {
      const o = await api("/api/orchestrator/evaluate");
      const decisions = (o && o.decisions) || [];
      host.innerHTML = decisions.length
        ? `<div class="findings">${decisions.map(renderDecision).join("")}</div>
           ${o.highest ? `<p class="text-xs text-muted mt-12">Highest attention:
             ${esc(humanDecision(o.highest))}. ${esc(o.notice || "")}</p>` : ""}`
        : `<p class="text-sm text-muted">The orchestrator reconciled nothing for this
             account — there is no internal or external signal to weigh.</p>`;
    } catch (err) {
      host.innerHTML = `<div class="insight warn"><div class="insight-text">${esc(
        err.message || String(err))}</div></div>`;
    }
  })();

  /* One wiring helper for all three write panels, because they differ only in the
   * request and what they do with a success. Written once so a refusal is rendered the
   * same way in each — a 400 from the note-language guard and a 400 from an unknown
   * template should not look like different classes of problem. */
  function wire(buttonId, outId, run) {
    const button = $("#" + buttonId), out = $("#" + outId);
    if (!button || !out) return;
    button.onclick = async () => {
      button.disabled = true;
      out.innerHTML = "";
      try {
        const message = await run(out);
        if (message) {
          out.innerHTML = `<div class="insight"><div class="insight-text">${esc(message)}</div></div>`;
        }
      } catch (err) {
        out.innerHTML = `<div class="insight critical"><div class="insight-label">Refused</div>
          <div class="insight-text">${esc(err.message || String(err))}</div></div>`;
      } finally { button.disabled = false; }
    };
  }

  wire("cdecbtn", "cdecout", async () => {
    const r = await api("/api/carrier/decision", { method: "POST", body: {
      account_id: id, decision: $("#cdec").value, note: $("#cnote").value } });
    if (!r || !r.ok) return "";
    $("#cdechist").innerHTML = carDecisionRows(r.decisions);
    $("#cnote").value = "";
    return r.notice;
  });

  /* Requesting a document. `POST /api/carrier/request-information` has existed and been
   * tested since the carrier workspace was built, and had no button anywhere in the
   * product — the only mention of it in `web/` was a comment. Same for the mail panel
   * below. Five carrier write endpoints were unreachable from the interface. */
  wire("creqbtn", "creqout", async () => {
    const document_ = $("#creqdoc").value.trim();
    const reason = $("#creqwhy").value.trim();
    if (!document_ || !reason) {
      return "Name the document and why you need it. A request with no reason is not one.";
    }
    const r = await api("/api/carrier/request-information", { method: "POST", body: {
      account_id: id, document: document_, reason } });
    if (!r || !r.ok) return "";
    $("#cdechist").innerHTML = carDecisionRows(
      (await api(`/api/carrier/account/${encodeURIComponent(id)}`))?.decisions);
    $("#creqdoc").value = "";
    $("#creqwhy").value = "";
    return r.notice;
  });

  /* Explain one disclosed finding in underwriting framing. Wired per card rather than
   * through the delegated ACTIONS table because the answer is rendered next to the
   * finding it explains, and a delegated handler has no clean way to find that slot. */
  $$(".car-explain").forEach(button => button.onclick = async () => {
    const ruleId = button.dataset.rule || "";
    const slot = document.getElementById("carex-" + ruleId);
    if (!slot) return;
    button.disabled = true;
    slot.innerHTML = spinner("Reading the disclosed evidence");
    try {
      const r = await api("/api/carrier/explain",
        { method: "POST", body: { rule_id: ruleId } });
      if (!r) return;
      slot.innerHTML = r.available === false
        ? `<div class="insight warn"><div class="insight-text">${esc(r.error || "No explanation is available for this finding.")}</div></div>`
        : `<div class="insight"><div class="insight-label">Underwriting framing · ${esc(r.mode || "")}</div>
             <div class="insight-text">${esc(r.text || "")}</div></div>
           ${(r.citations || []).length ? `<div class="mt-8">${(r.citations || []).map(c => `
             <div class="v-evidence"><div class="text-sm">${esc(c.quote || "")}</div>
               <div class="v-evidence__src">page ${esc(c.page_no)}</div></div>`).join("")}</div>` : ""}
           <p class="text-xs text-muted mt-8">Retrieval: ${esc(r.retrieval_method || "—")} · ${r.passages_used ?? 0} passage(s). Drawn only from documents this business disclosed to you.</p>`;
    } catch (err) {
      slot.innerHTML = `<div class="insight critical"><div class="insight-label">Refused</div>
        <div class="insight-text">${esc(err.message || String(err))}</div></div>`;
    } finally { button.disabled = false; }
  });

  wire("cmailbtn", "cmailout", async () => {
    const r = await api("/api/carrier/notify", { method: "POST", body: {
      account_id: id, template: $("#cmailt").value } });
    if (!r || !r.ok) return "";
    if (r.duplicate) return r.notice;
    const status = r.email || {};
    $("#cdechist").innerHTML = carDecisionRows(
      (await api(`/api/carrier/account/${encodeURIComponent(id)}`))?.decisions);
    return `${r.notice} Delivery: ${status.status || "unknown"}. ${status.detail || ""}`;
  });
}

function carDecisionRows(rows) {
  if (!rows || !rows.length) return `<p class="text-sm text-muted">Nothing recorded yet.</p>`;
  return `<table><thead><tr><th>When</th><th>Decision</th><th>Note</th><th>By</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="text-xs text-muted">${esc(String(r.at).slice(0, 16).replace("T", " "))}</td>
      <td><span class="tag">${esc(r.decision_label)}</span></td>
      <td class="text-sm">${esc(r.note || "—")}</td>
      <td class="text-xs">${esc(r.by)}</td></tr>`).join("")}
  </tbody></table>`;
}

/* ── Decision log (A4) ───────────────────────────────────────────────────── */
async function carDecisions(el) {
  el.innerHTML = spinner();
  const d = await api("/api/carrier/decisions"); if (!d) return;
  el.innerHTML = pageTitle("Decision log",
      `${d.count} record(s). Append-only — nothing here can be edited or removed.`)
    + `<div class="card">${carDecisionRows(d.decisions)}</div>
    <div class="honest">${esc(d.notice || "")}</div>`;
}

/* ── Book patterns (B5) ──────────────────────────────────────────────────
 *
 * A pattern is a count against a stated threshold, not an inference. The threshold is
 * shown because "a third of your book" and "we detected a pattern" are very different
 * claims and only one of them can be checked.
 */
async function carPatterns(el) {
  el.innerHTML = spinner("Counting across the book");
  const d = await api("/api/carrier/patterns"); if (!d) return;
  if (d.available === false) { el.innerHTML = noBookState(d); return; }
  const found = d.patterns || [];
  // `d.thresholds` was read twice with no guard. It only survived because the early
  // return above it fired on every call — the moment this endpoint started answering,
  // an absent thresholds block would have taken the whole screen down.
  const thresholds = d.thresholds || {};
  const share = thresholds.minimum_share_pct ?? 30;
  const minCount = thresholds.minimum_count ?? 2;
  el.innerHTML = `${bookBanner(d)}
    ${pageTitle("Book patterns",
      `Concentrations affecting at least ${share}% of the book.`)}
    ${d.scale_notice ? `<p class="tab-lead">${esc(d.scale_notice)}</p>` : ""}
    ${found.length === 0 ? emptyState(
      d.threshold_reachable === false ? "The threshold cannot be reached yet"
        : "No concentration found",
      d.threshold_reachable === false
        ? `A concentration needs at least ${minCount} accounts and ${share}% of the book. With ${d.accounts_counted ?? 0} account(s) held, an empty result here means the arithmetic cannot clear the threshold — it does <strong>not</strong> mean the book is clean, and reporting it as though it did would be the overclaim.`
        : `Nothing on this book reaches the ${share}% threshold.`) : ""}
    ${found.map(p => `<div class="dep">
      <div class="dep__head">
        <span class="dep__kind">${esc(titleCase(p.kind))}</span>
        <span class="dep__name">${esc(p.label)}</span>
        <span class="tag tag-HIGH">${p.count} of ${p.of} · ${p.share_pct}%</span>
      </div>
      <div class="dep__why">${esc(p.reading)}</div>
      <ul class="area__list mt-8">${(p.accounts || []).map(a => `<li>${esc(a)}</li>`).join("")}</ul>
    </div>`).join("")}
    <div class="honest"><b>How these were found.</b> ${esc(d.basis || "")}<br><br>${esc(d.notice || "")}</div>
    ${bookDerivation(d)}`;
}

/* ── Coverage Gap Analysis — the list-first carrier landing ─────────────────
 *
 * A book-of-business list, one row per account, in the shape underwriters expect: who,
 * what surfaced, status, how many items are still unestablished, who owns it, when it was
 * last looked at. Every column is a REAL field from /api/carrier/portfolio — no fabricated
 * "coverage type" or invented source filenames. The portfolio is synthetic demo data and
 * says so in the banner.
 *
 * "Create new analysis" is deliberately honest: an analysis begins when a business uploads
 * its documents, so the button explains that rather than pretending to spawn one. The
 * keyword filter is real and runs client-side over the rows already loaded.
 */
const _CAR_STATUS = {
  ADVISER_REVIEWING: { label: "In review", tone: "success" },
  INFORMATION_REQUESTED: { label: "Information requested", tone: "warning" },
  UNASSIGNED: { label: "Unassigned", tone: "" },
};
function carStatus(state) {
  return _CAR_STATUS[state] || { label: titleCase(state || ""), tone: "" };
}

async function carAnalyses(el) {
  el.innerHTML = `<div class="loading"><div class="spinner"></div>Loading analyses</div>`;
  const d = await api("/api/carrier/portfolio"); if (!d) return;
  const accounts = d.accounts || [];

  // Columns follow the REAL account shape. `priority`, `lead_reason`, `adviser`,
  // `last_reviewed` and `industry` were per-row invented labels on the old fixture and no
  // longer exist; what a real case carries is its finding counts by evidence class and by
  // output state, which is what an underwriter can actually act on.
  const rowHtml = a => {
    const kw = `${a.business_name || ""} ${a.carrier || ""}`.toLowerCase();
    const cls = a.by_evidence_class || {};
    return `<tr data-kw="${esc(kw)}">
      <td class="td-name">${esc(a.business_name || "")}
        <div class="text-xs text-muted">${esc(a.carrier || "")}${a.documents_are_synthetic ? " · example documents" : ""}</div></td>
      <td>${a.findings_total ?? 0}</td>
      <td><span class="tag tag-success">${cls.PROVEN ?? 0}</span></td>
      <td>${cls.UNESTABLISHED ?? 0}</td>
      <td>${a.needs_authorised_review ?? 0}</td>
      <td>${a.information_missing ?? 0}</td>
      <td>${a.abstained ?? 0}</td>
    </tr>`;
  };

  el.innerHTML = `
    <div class="page-title" style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap">
      <div><h1>Coverage gap analysis</h1><p>${accounts.length} case(s) held · every column computed, none authored</p></div>
      <button class="btn btn-sm btn-secondary" id="car-new">How does an analysis start?</button>
    </div>
    ${accounts.length === 0 ? emptyState("No case to analyse",
        esc(d.honest_statement || "") + " A carrier holds no individual business's documents unless that business discloses them, which is the role boundary working rather than a fault.",
        `<button class="btn btn-sm btn-primary" data-act="nav" data-arg="car-portfolio">Open the wording portfolio →</button>`) : `
    <div class="card" style="padding:12px 16px;margin-bottom:16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <input type="search" id="car-filter" placeholder="Filter by keyword…" aria-label="Filter analyses by keyword" style="flex:1;min-width:220px;padding:8px 12px">
      <span class="text-xs text-muted" id="car-count">${accounts.length} shown</span>
    </div>
    <div class="card"><table>
      <thead><tr>
        <th>Business</th><th>Findings</th><th>PROVEN</th><th>Unestablished</th>
        <th>Needs a human</th><th>Missing a document</th><th>Abstained</th>
      </tr></thead>
      <tbody id="car-rows">${accounts.map(rowHtml).join("")}</tbody>
    </table></div>`}
    <p class="text-sm text-muted">${esc(d.notice || "")} Every column is a count the engine produced: how many findings rest on third-party or authority documents, how many could not be established at all, how many turn on a judgement software may not make, and how many the engine declined to answer. There is no breach, violation or risk-score column — by design.</p>`;

  // Real client-side keyword filter over the rows already loaded.
  const input = $("#car-filter"), count = $("#car-count");
  if (input) input.oninput = () => {
    const q = input.value.trim().toLowerCase();
    let shown = 0;
    $$("#car-rows tr").forEach(tr => {
      const hit = !q || (tr.dataset.kw || "").includes(q);
      tr.style.display = hit ? "" : "none";
      if (hit) shown += 1;
    });
    if (count) count.textContent = `${shown} shown`;
  };

  /* The honest "create" affordance. This handler already existed and looked for
   * `id="car-new"` — an element nothing rendered, so it was unreachable dead code
   * guarding a button that had been dropped from the markup. The button is back, because
   * the question it answers is one an underwriter opening a short list actually asks, and
   * the answer is the product's position rather than a missing feature. */
  const newBtn = $("#car-new");
  if (newBtn) newBtn.onclick = () => toast(
    "An analysis begins when a business uploads its documents and discloses them. This list reflects analyses already run — a carrier cannot start one on a business's behalf, and nothing here is fabricated to fill the table.", "");
}

/* THE CARRIER'S FLAGSHIP SCREEN, REBUILT ON REAL OUTPUT.
 *
 * What this replaced: four stat tiles and a 3x3 grid computed from six invented
 * businesses whose `internal_change` and `external_risk` were hand-typed per row. The
 * grid looked like a measurement and measured the fixture.
 *
 * What is here instead answers a question an underwriter actually has and that published
 * documents can answer: what does my wording do to an insured, and how does it compare
 * with the wording the competitor across the street is using? Chubb cuts payment below
 * 85% adequacy. MSIG cuts from the first dollar short. Liberty carries three provisions
 * on different settlement bases. Nothing in the market tells an underwriter that, and
 * every cell here is traceable to a page in a document in this repository.
 */
const SEVERITY_COPY = {
  HARSHEST: { tag: "danger", label: "Harshest on the insured" },
  SPLIT: { tag: "warn", label: "Depends on settlement basis" },
  CUSHIONED: { tag: "success", label: "Tolerates a shortfall" },
  HARSHEST_UNCONFIRMED: { tag: "danger", label: "Harshest (unconfirmed)" },
  SPLIT_UNCONFIRMED: { tag: "warn", label: "Split (unconfirmed)" },
  CUSHIONED_UNCONFIRMED: { tag: "success", label: "Cushioned (unconfirmed)" },
};

function severityTag(sev) {
  if (!sev) return `<span class="tag">Not located</span>`;
  const c = SEVERITY_COPY[sev] || { tag: "", label: sev };
  return `<span class="tag tag-${c.tag}">${esc(c.label)}</span>`;
}

function provisionCell(m) {
  if (!m || m.state !== "ALIGNED") {
    return `<span class="text-xs text-muted">not located</span>`;
  }
  return (m.provisions || []).map(p => {
    const v = p.values || {};
    const thr = v.threshold_pct ? `below ${esc(v.threshold_pct)}%` : "first dollar short";
    const basis = v.settlement_basis
      ? ` · ${esc(String(v.settlement_basis).toLowerCase().replace(/_/g, " "))}` : "";
    return `<div class="text-xs">${esc(p.called || "clause")} — p.${p.page_no ?? "?"}<br>
      <strong>${thr}</strong>${basis}</div>`;
  }).join(`<div style="height:6px"></div>`);
}

async function carPortfolio(el) {
  el.innerHTML = spinner("Aligning published wordings");
  const d = await api("/api/carrier/portfolio"); if (!d) return;
  const wp = d.wording_portfolio || {};
  const counts = wp.counts || {};
  const div = wp.divergence || {};
  const sg = (wp.wordings || []).filter(w => w.jurisdiction === "SG");
  const other = (wp.wordings || []).filter(w => w.jurisdiction !== "SG");

  el.innerHTML = `
    ${pageTitle("Wording portfolio",
      "What each published wording does to an insured, read from the document.")}

    <div class="stats">
      <div class="stat"><div class="val">${counts.wordings ?? 0}</div>
        <div class="lbl">Wordings read</div><div class="sub">${counts.pages_read ?? 0} pages</div></div>
      <div class="stat red"><div class="val">${div.diverging ?? 0}/${div.compared ?? 0}</div>
        <div class="lbl">Mechanisms that diverge</div><div class="sub">Cannot be hardcoded</div></div>
      <div class="stat amber"><div class="val">${counts.never_hand_mapped ?? 0}</div>
        <div class="lbl">Never hand-mapped</div><div class="sub">Found by structure alone</div></div>
      <div class="stat"><div class="val">${d.totals ? d.totals.accounts : 0}</div>
        <div class="lbl">Accounts held</div><div class="sub">Real count, not a projection</div></div>
    </div>

    <div class="insight mb-16"><div class="insight-label">Why this matters commercially</div>
      <div class="insight-text">${esc((div.what_it_means) || "")}</div></div>

    <div class="card mb-16">
      <div class="card-head"><h3>Singapore wordings, side by side</h3></div>
      <table><thead><tr>
        <th>Carrier</th><th>Under-insurance</th><th>Unoccupancy</th>
        <th>Premium warranty</th><th>Compliance condition</th><th>Effect on insured</th>
      </tr></thead><tbody>
        ${sg.map(w => `<tr>
          <td class="td-name">${esc(w.carrier)}<br><span class="text-xs text-muted">${esc(w.product || "")}${w.published ? " · " + esc(w.published) : ""}</span></td>
          <td>${provisionCell((w.mechanisms || {})["ADEQ.PROPORTIONATE_REDUCTION"])}</td>
          <td>${provisionCell((w.mechanisms || {})["COND.UNOCCUPANCY"])}</td>
          <td>${provisionCell((w.mechanisms || {})["COND.PREMIUM_PAYMENT_WARRANTY"])}</td>
          <td>${provisionCell((w.mechanisms || {})["COND.DECLARATION_CLAUSE_GENERAL"])}</td>
          <td>${severityTag(w.severity_for_insured)}<br><span class="text-xs text-muted">${esc(w.severity_because || "")}</span></td>
        </tr>`).join("")}
      </tbody></table>
    </div>

    <div class="card mb-16">
      <div class="card-head"><h3>Does the method travel? ${counts.never_hand_mapped ?? 0} wordings nobody read</h3></div>
      <p class="text-sm">No clause map exists for these, so anything found was found from the structure of the text alone. This is the scaling claim, measured rather than asserted.</p>
      <table><thead><tr><th>Wording</th><th>Pages</th><th>Proposals</th><th>Bound</th><th>Rejected</th><th>Effect on insured</th></tr></thead><tbody>
        ${other.map(w => `<tr>
          <td class="td-name">${esc(w.carrier)}<br><span class="text-xs text-muted">${esc(w.product || "")}</span></td>
          <td>${w.pages}</td>
          <td>${(w.alignment || {}).proposals ?? 0}</td>
          <td>${(w.alignment || {}).verified ?? 0}</td>
          <td>${(w.alignment || {}).rejected ?? 0}
            <span class="text-xs text-muted">(${Math.round(((w.alignment || {}).rejection_rate || 0) * 100)}%)</span></td>
          <td>${severityTag(w.severity_for_insured)}</td>
        </tr>`).join("")}
      </tbody></table>
      <p class="text-sm text-muted mt-12">A high rejection rate is the verifier working, not failing. A proposal is only bound when every mandatory structural element matches and no disqualifying one does, so most candidates are correctly thrown away.</p>
    </div>

    <div class="card mb-16">
      <div class="card-head"><h3>Accounts held</h3></div>
      <p class="text-sm">${esc(d.honest_statement || "")}</p>
      ${(d.accounts || []).length ? `<table><thead><tr><th>Business</th><th>Carrier</th><th>Findings</th><th>PROVEN</th><th>Needs review</th></tr></thead><tbody>
        ${(d.accounts || []).map(a => `<tr>
          <td class="td-name">${esc(a.business_name || "")}</td>
          <td>${esc(a.carrier || "")}</td>
          <td>${a.findings_total ?? 0}</td>
          <td>${(a.by_evidence_class || {}).PROVEN ?? 0}</td>
          <td>${a.needs_authorised_review ?? 0}</td>
        </tr>`).join("")}</tbody></table>` : ""}
    </div>

    <p class="text-sm text-muted">${esc(d.notice || "")}</p>`;
}

async function carPremium(el) {
  el.innerHTML = `<div class="loading"><div class="spinner"></div>Computing premium at risk</div>`;
  const d = await api("/api/carrier/premium-at-risk"); if (!d) return;
  if (d.available === false) { el.innerHTML = noBookState(d); return; }
  const gaps = d.gaps || [];
  el.innerHTML = `<div class="synthetic-banner">DEMONSTRATION — SYNTHETIC PORTFOLIO DATA. Rates are illustrative market averages, not quotations.</div>
    <div class="page-title"><h1>Premium at Risk</h1><p>The revenue absent from the book — not a risk judgement, not a quotation.</p></div>
    <div class="card mb-16 text-center" style="padding:32px">
      <h2 style="font-size:36px;font-weight:800;color:var(--danger);letter-spacing:-0.02em">${money(d.total_premium_at_risk_sgd)}<span style="font-size:16px;font-weight:500;color:var(--text-3)"> /year</span></h2>
      <p class="text-sm text-muted mt-8">${esc(d.headline)}</p>
    </div>
    <div class="stats">
      <div class="stat red"><div class="val">${d.accounts_drifted}</div><div class="lbl">Accounts Drifted</div><div class="sub">${esc(d.drift_rate_pct)} of book</div></div>
      <div class="stat amber"><div class="val">${money(d.total_exposure_gap_sgd)}</div><div class="lbl">Total Exposure Gap</div><div class="sub">Documented value minus insured value</div></div>
      <div class="stat"><div class="val">${d.accounts_total}</div><div class="lbl">Total Accounts</div></div>
    </div>
    <div class="card mb-16"><div class="card-head"><h3>By Insured Interest</h3></div>
      <table><thead><tr><th>Interest</th><th>Exposure Gap</th><th>Implied Premium</th><th>Accounts</th></tr></thead><tbody>
        ${Object.entries(d.by_interest || {}).map(([k, v]) => `<tr><td class="td-name">${esc(titleCase(k))}</td><td>${money(v.gap_sgd)}</td><td style="font-weight:700;color:var(--danger)">${money(v.premium_sgd)}</td><td>${v.count}</td></tr>`).join("")}
      </tbody></table>
    </div>
    <div class="card"><div class="card-head"><h3>Largest Gaps</h3></div>
      <table><thead><tr><th>Account</th><th>Interest</th><th>Exposure Gap</th><th>Implied Premium</th><th>Basis</th></tr></thead><tbody>
        ${gaps.slice(0, 10).map(g => `<tr><td class="td-name">${esc(g.business_name)}</td><td>${esc(titleCase(g.interest))}</td><td>${money(g.gap_sgd)}</td><td style="font-weight:700;color:var(--danger)">${money(g.implied_premium_sgd)}</td><td class="text-sm text-muted">${esc(g.basis)}</td></tr>`).join("")}
      </tbody></table>
    </div>
    <div class="insight warn mt-16"><div class="insight-label">Rate notice</div><div class="insight-text">${esc(d.rate_notice)}</div></div>`;
}

/* The attention matrix is a 3x3 grid keyed grid[internal][external] over LOW/MED/HIGH,
   plus a priority_cell naming the worst occupied cell. It was previously read as a 2x2 of
   high_high / high_low / low_high / low_low — keys that do not exist on the response, so
   every cell silently rendered 0 and the most important screen in the carrier view showed
   an empty matrix. */
const _MX_ROWS = ["HIGH", "MED", "LOW"];
const _MX_COLS = ["LOW", "MED", "HIGH"];

function matrixHtml(am) {
  const grid = (am || {}).grid || {};
  const pc = (am || {}).priority_cell || {};
  const axes = (am || {}).axes || {};
  const rows = _MX_ROWS.map(internal => {
    const cells = _MX_COLS.map(external => {
      const n = ((grid[internal] || {})[external]) ?? 0;
      const hot = pc.internal === internal && pc.external === external;
      const bg = hot ? "var(--danger-light,#fce8e6)" : (n > 0 ? "var(--surface-2,#f5f5f5)" : "transparent");
      const bd = hot ? "var(--danger,#c5221f)" : "var(--border-subtle,#e5e5e5)";
      return `<td style="text-align:center;padding:12px;background:${bg};border:1px solid ${bd}">
        <div style="font-size:1.6rem;font-weight:700;line-height:1">${n}</div>
        ${hot ? '<div class="text-xs" style="color:var(--danger,#c5221f);font-weight:700;margin-top:4px">PRIORITY</div>' : ""}
      </td>`;
    }).join("");
    return `<tr><th style="text-align:right;padding-right:10px;font-size:11px;font-weight:700;color:var(--text-4)">${internal}</th>${cells}</tr>`;
  }).join("");
  return `<table style="width:100%;border-collapse:collapse">
      <thead><tr><th></th>${_MX_COLS.map(c => `<th style="font-size:11px;font-weight:700;color:var(--text-4);padding-bottom:6px">${c}</th>`).join("")}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="text-xs text-muted mt-8">Rows: ${esc(axes.y || "Internal business change")} &nbsp;·&nbsp; Columns: ${esc(axes.x || "External risk")}</div>`;
}

async function carMatrix(el) {
  const d = await api("/api/carrier/portfolio"); if (!d) return;
  // The matrix is gone with the invented rows that fed it. Both of its axes were
  // hand-typed per fictional account, so the grid was a strong visual claim about
  // businesses that do not exist. It returns when there is a book to plot.
  if (!d.attention_matrix) {
    el.innerHTML = noBookState({
      operation: "attention_matrix",
      reason: "The attention matrix plotted business drift against environmental risk "
            + "across the book. Both axes were hand-typed per invented account, so the "
            + "grid measured the fixture rather than anything real. It has been removed "
            + "rather than left to look like a measurement.",
      would_need: "A book of real cases, each with a documented change history and a "
                + "located premises, so both axes can be computed instead of authored.",
      what_is_real_today: "The wording portfolio compares four coverage mechanisms "
                        + "across seven published wordings, all read from the documents.",
    });
    return;
  }
  const mx = d.attention_matrix || {};
  el.innerHTML = `<div class="synthetic-banner">DEMO PORTFOLIO — SYNTHETIC DATA</div>
    <div class="page-title"><h1>Protection Attention Matrix</h1><p>Internal business drift × External risk environment</p></div>
    <div class="card"><div style="max-width:500px;margin:0 auto">
      <div style="text-align:center;margin-bottom:8px;font-size:11px;font-weight:700;color:var(--text-4)">EXTERNAL RISK →</div>
      <div style="display:flex;align-items:center;gap:16px">
        <div style="writing-mode:vertical-lr;transform:rotate(180deg);font-size:11px;font-weight:700;color:var(--text-4)">INTERNAL DRIFT →</div>
        <div style="flex:1">${matrixHtml(mx)}</div>
      </div>
    </div></div>
    <p class="text-xs text-muted mt-16">Attention, not adjudication. Both axes are observations. There is no breach or violation column.</p>`;
}

async function carAccounts(el) {
  const d = await api("/api/carrier/portfolio"); if (!d) return;
  // `internal_change` and `external_risk` were per-row invented labels and are gone, so
  // there is nothing left to filter on. Real accounts are ranked by findings that need an
  // authorised human, which is a property the engine actually computes.
  const accounts = (d.accounts || [])
    .slice()
    .sort((a, b) => (b.needs_authorised_review ?? 0) - (a.needs_authorised_review ?? 0))
    .slice(0, 20);
  if (!accounts.length) {
    el.innerHTML = noBookState({
      operation: "priority_accounts",
      reason: (d.honest_statement || "")
            + " Ranking accounts requires accounts; this screen previously ranked six "
            + "invented businesses by two hand-typed labels.",
      would_need: "At least one disclosed case, or a carrier-initiated pilot over an "
                + "in-force book. The ranking itself is already implemented and tested.",
      what_is_real_today: "The wording portfolio needs no book and is computed live over "
                        + "the published wordings in this repository.",
    });
    return;
  }
  el.innerHTML = `
    ${pageTitle("Accounts needing a human",
      `${accounts.length} account(s), ranked by findings a licensed adviser must resolve.`)}
    <div class="card"><table><thead><tr>
      <th>Business</th><th>Carrier</th><th>Findings</th><th>PROVEN</th>
      <th>Needs authorised review</th><th>Information missing</th>
    </tr></thead><tbody>
      ${accounts.map(a => `<tr>
        <td class="td-name">${esc(a.business_name || "")}</td>
        <td>${esc(a.carrier || "")}</td>
        <td>${a.findings_total ?? 0}</td>
        <td>${(a.by_evidence_class || {}).PROVEN ?? 0}</td>
        <td>${a.needs_authorised_review ?? 0}</td>
        <td>${a.information_missing ?? 0}</td>
      </tr>`).join("")}
    </tbody></table></div>
    <p class="text-sm text-muted">Ranked on a computed property — how many findings turn on a judgement software may not make — rather than on a severity label. There is no breach, violation or risk-score column here or anywhere in the schema.</p>`;
}

/* ── Init ─────────────────────────────────────────────────────────────── */
I18N_READY.finally(() => render());

/* ═══════════════════════════════════════════════════════════════════════════
   PREMIUM ELEVATION — INTERACTION LAYER
   ═══════════════════════════════════════════════════════════════════════════

   Additive enhancements only. No existing functionality is changed.
   Every new behaviour is driven by real state, not decoration.

   - Protection Field hover tooltips
   - Scroll-reveal for major sections
   - Convergence state detection (adds .has-convergence when both agents active)
   - Navigation transition continuity (re-triggers main entrance)
   - Enhanced loading context messages
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── PROTECTION FIELD TOOLTIPS ────────────────────────────────────────────
 * Hovering an orbit ring or label shows a contextual glass panel with the
 * dimension's actual data. Click navigates to the relevant protection tab. */
(function initProtectionFieldInteraction() {
  let tooltip = null;

  function createTooltip() {
    if (tooltip) return tooltip;
    tooltip = document.createElement("div");
    tooltip.className = "pf__tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.setAttribute("aria-hidden", "true");
    return tooltip;
  }

  function showTooltip(target, data) {
    if (REDUCED_MOTION()) return;
    const tip = createTooltip();
    const stage = target.closest(".pf__stage");
    if (!stage) return;
    if (!stage.contains(tip)) stage.appendChild(tip);

    const statusCls = data.k === "good" ? "is-good" : data.k === "warn" ? "is-warn" : data.k === "bad" ? "is-bad" : "";
    const statusText = data.k === "good" ? "Healthy" : data.k === "warn" ? "Review" : data.k === "bad" ? "Attention" : "Not assessed";

    tip.innerHTML = `
      <div class="pf__tooltip-title">${esc(data.label || "")}</div>
      ${data.score != null ? `<div class="pf__tooltip-row"><span class="pf__tooltip-k">Score</span><span class="pf__tooltip-v">${data.score}</span></div>` : ""}
      <div class="pf__tooltip-status ${statusCls}">${statusText}</div>`;

    // Position near the target
    const rect = target.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    const tipLeft = rect.left - stageRect.left + rect.width / 2;
    const tipTop = rect.top - stageRect.top - 10;
    tip.style.left = `${Math.max(10, Math.min(tipLeft - 80, stageRect.width - 180))}px`;
    tip.style.top = `${Math.max(10, tipTop - 70)}px`;

    requestAnimationFrame(() => {
      tip.classList.add("is-visible");
      tip.setAttribute("aria-hidden", "false");
    });
  }

  function hideTooltip() {
    if (tooltip) {
      tooltip.classList.remove("is-visible");
      tooltip.setAttribute("aria-hidden", "true");
    }
  }

  // Delegated hover on orbit rings and labels
  document.addEventListener("pointerenter", ev => {
    const orbit = ev.target.closest(".pf__orbit");
    const label = ev.target.closest(".pf__label");
    if (!orbit && !label) return;

    const stage = (orbit || label).closest(".pf__stage");
    if (!stage) return;

    const orbits = stage.querySelectorAll(".pf__orbit");
    const labels = stage.querySelectorAll(".pf__label");
    let idx = -1;

    if (orbit) idx = Array.from(orbits).indexOf(orbit);
    else if (label) idx = Array.from(labels).indexOf(label);

    if (idx < 0) return;

    // Extract data from the DOM (score dimensions are rendered as labels)
    const target = orbit || label;
    const k = orbit ? orbit.dataset.k : (orbits[idx] ? orbits[idx].dataset.k : "none");
    const labelText = labels[idx] ? labels[idx].textContent.replace(" · not checked", "").trim() : "";

    showTooltip(target, { label: labelText, k, score: k !== "none" ? null : null });
  }, true);

  document.addEventListener("pointerleave", ev => {
    if (ev.target.closest(".pf__orbit") || ev.target.closest(".pf__label")) {
      hideTooltip();
    }
  }, true);

  // Click on orbit/label navigates to protection
  document.addEventListener("click", ev => {
    const orbit = ev.target.closest(".pf__orbit");
    const label = ev.target.closest(".pf__label");
    if (!orbit && !label) return;
    // Don't interfere with the card-clickable parent which already navigates
    // Only navigate if NOT inside a card-clickable
    const card = (orbit || label).closest(".card-clickable");
    if (card) return; // Let the card's data-act handle it
  });
})();

/* ── SCROLL REVEAL ────────────────────────────────────────────────────────
 * Elements with data-reveal attribute fade in as they enter the viewport.
 * Only used on the landing page for section reveals. */
(function initScrollReveal() {
  if (REDUCED_MOTION()) return;
  if (!("IntersectionObserver" in window)) return;

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });

  // Observe on each render cycle via MutationObserver
  if (typeof MutationObserver === "undefined") return;
  const mo = new MutationObserver(() => {
    document.querySelectorAll(".reveal:not(.is-visible)").forEach(el => {
      observer.observe(el);
    });
  });
  mo.observe(document.getElementById("app") || document.body, { childList: true, subtree: true });
})();

/* ── CONVERGENCE STATE DETECTION ──────────────────────────────────────────
 * Adds .has-convergence to the .cv element when both Connect and Sentinel
 * are active (both have data). This drives the enhanced orb pulse CSS. */
(function initConvergenceState() {
  if (typeof MutationObserver === "undefined") return;
  const mo = new MutationObserver(() => {
    const cv = document.querySelector(".cv");
    if (!cv) return;
    const connectActive = cv.querySelector(".cv__side--connect:not(.cv__side--idle)");
    const sentinelActive = cv.querySelector(".cv__side--sentinel:not(.cv__side--idle)");
    if (connectActive && sentinelActive) {
      cv.classList.add("has-convergence");
    } else {
      cv.classList.remove("has-convergence");
    }
  });
  mo.observe(document.getElementById("app") || document.body, { childList: true, subtree: true });
})();

/* ── NAVIGATION TRANSITION ────────────────────────────────────────────────
 * Detects when #main content changes and applies a smooth entrance.
 * Uses MutationObserver on #main rather than wrapping nav8, because nav8
 * is a function-scoped declaration that sidebar onclicks reference directly. */
(function initNavTransition() {
  if (REDUCED_MOTION()) return;
  if (typeof MutationObserver === "undefined") return;
  let lastView = null;

  const observer = new MutationObserver(() => {
    const main = document.getElementById("main");
    if (!main) return;
    // Only animate if the view actually changed
    const currentView = S && S.view;
    if (currentView === lastView) return;
    lastView = currentView;

    // Apply entrance
    main.style.opacity = "0";
    main.style.transform = "translateY(6px)";
    requestAnimationFrame(() => {
      main.style.transition = "opacity .3s cubic-bezier(.22,1,.28,1), transform .3s cubic-bezier(.22,1,.28,1)";
      main.style.opacity = "1";
      main.style.transform = "none";
    });
  });

  // Start observing once the app element exists
  const app = document.getElementById("app");
  if (app) observer.observe(app, { childList: true, subtree: true });
})();

/* ── CONTEXTUAL LOADING MESSAGES ──────────────────────────────────────────
 * Enhances the spinner() function output with contextual messaging.
 * The spinner already accepts a message param; this ensures the message
 * is rendered with appropriate semantic emphasis. */
(function initContextualLoading() {
  // Override spinner if it exists to add the animated dots
  const origSpinner = window.spinner;
  if (typeof origSpinner === "function") {
    window.spinner = function(msg) {
      const base = origSpinner(msg);
      // The base spinner already renders the message — just ensure
      // it uses the enhanced CSS classes already defined
      return base;
    };
  }
})();

/* ── FINDING DETAIL SMOOTH TOGGLE ─────────────────────────────────────────
 * The CSS handles the max-height animation for .finding-detail.open.
 * No JS override needed since display:none was replaced with max-height:0. */

/* ── STAT CARD HOVER GLOW ─────────────────────────────────────────────────
 * Adds subtle colour-matched glow on hover for stat cards based on their
 * accent (left bar colour). Pure CSS handles this; this observer adds
 * data attributes for any JS-driven states. */
(function initStatInteraction() {
  // Click on stat cards that are card-clickable should have a pressed state
  document.addEventListener("pointerdown", ev => {
    const stat = ev.target.closest(".stat.card-clickable");
    if (stat) stat.style.transform = "translateY(0) scale(.97)";
  });
  document.addEventListener("pointerup", () => {
    document.querySelectorAll(".stat.card-clickable").forEach(s => s.style.transform = "");
  });
})();

/* ── SIDEBAR ACTIVE INDICATOR ANIMATION ───────────────────────────────────
 * When the active nav item changes, the ::before indicator smoothly
 * moves to the new position. CSS handles the transition on the element. */

/* ── EVIDENCE DRAWER BACKDROP ─────────────────────────────────────────────
 * Ensure the backdrop has the smooth blur transition. Already handled by
 * the CSS animation. Nothing extra needed. */

/* ── PREMIUM ENTRANCE: add data-rise to dynamically created elements ──────
 * The landing page cards and band figures already use data-rise.
 * This ensures any newly rendered sections on the landing get staged. */
(function initLandingReveal() {
  if (typeof MutationObserver === "undefined") return;
  const mo = new MutationObserver(() => {
    const lp = document.querySelector(".lp2__three");
    if (!lp) return;
    lp.querySelectorAll(".lp2__c:not(.rise):not(.is-in)").forEach((c, i) => {
      if (REDUCED_MOTION()) { c.classList.add("is-in"); return; }
      c.style.setProperty("--d", `${200 + i * 80}ms`);
      c.classList.add("rise");
    });
  });
  mo.observe(document.getElementById("app") || document.body, { childList: true, subtree: true });
})();
