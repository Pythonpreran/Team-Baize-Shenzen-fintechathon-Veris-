"""Hock Lee Foods Pte Ltd — the customer's own public website.

THIS IS NOT PRODUCT CODE. It lives outside `src/proven/` on purpose.

WHY IT EXISTS. The Discovery agent's whole claim is that it finds a business change from
OUTSIDE the business's own software — a company opens a second unit, updates its website,
and never tells its insurer. Demonstrating that needs a website to actually fetch. Hock
Lee Foods is a fictional company, so `hockleefoods.com.sg` does not resolve, and every
live Discovery run against it failed with `getaddrinfo failed`. The agent was reaching for
a page that could not exist.

So this serves that page, on its own port, as a separate process. What the product does to
it is a genuine outbound HTTP GET over the network stack — no fixture, no special case, no
import. Point `PROVEN_DEMO_SITE_URL` somewhere else and Discovery will fetch that instead;
nothing in the product knows this file exists.

WHAT IS REAL AND WHAT IS A STAND-IN, stated plainly because the product's entire pitch is
that it does not blur this:
  * REAL      the HTTP request, the HTML parsing, the extraction, the grounding gate,
              the change detection and the notification.
  * STAND-IN  the company. There is no Hock Lee Foods, so this site plays the part of
              the page its owner would have written.

THE OPERATOR PANEL is the point. `/operator` opens a branch, closes one, or changes the
team size, and writes it to `state.json`. That is the twenty-second demonstration: change
the website, re-run Discovery, watch the alert arrive. Without a mutable site you can only
show a static finding, which proves nothing about noticing a CHANGE.

Run it:
    python demo-site/site.py
    # or: uvicorn site:app --port 8100 --app-dir demo-site
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

STATE_PATH = Path(__file__).with_name("state.json")
_LOCK = threading.RLock()

PORT = 8100

# ── the baseline ─────────────────────────────────────────────────────────────
#
# Deliberately matched to the old hard-coded fixture in discovery.py: units 03-11 and
# 03-12 and "a team of 14". Two reasons. It keeps the demo narrative that already works
# (the policy schedule lists ONE unit and declares 11 staff, so there is something worth
# finding on the very first look), and it means swapping the fixture for this live site
# changes where the text comes from without changing what the agent concludes.
#
# The branches the operator panel can open are NOT in the baseline. They are the change.

BASELINE: dict = {
    "company": {
        "name": "Hock Lee Foods Pte Ltd",
        "uen": "T09HL1234A",
        "tagline": "Singapore's trusted prepared meals manufacturer",
        "since": 2009,
        "phone": "6789 0123",
        "email": "info@hockleefoods.com.sg",
    },
    "headcount": 14,
    "locations": [
        {
            "id": "tai-seng-03-11",
            "label": "Main Production Kitchen",
            "unit": "#03-11",
            "street": "21 Tai Seng Avenue",
            "postal": "534054",
            "opened": "March 2009",
            "note": "Head office and primary production floor.",
            "published": True,
        },
        {
            "id": "tai-seng-03-12",
            "label": "Cold Storage & Distribution",
            "unit": "#03-12",
            "street": "21 Tai Seng Avenue",
            "postal": "534054",
            "opened": "June 2026",
            "note": "Chilled and frozen holding, order consolidation.",
            "published": True,
        },
    ],
    "activities": [
        "Manufacture of prepared meals and dishes",
        "Wholesale supply to food service and catering",
        "Retail sale via internet and online ordering platforms",
        "Cold-chain storage and distribution",
    ],
    "equipment": [
        "8-tray blast freezer commissioned 2026",
        "Two chilled delivery vans",
    ],
    "news": [
        {"date": "2026-06-01",
         "title": "Cold storage unit now operating at Tai Seng",
         "body": "Our new chilled and frozen holding area at #03-12 is now handling "
                 "order consolidation for wholesale customers."},
        {"date": "2026-03-14",
         "title": "New blast freezer commissioned",
         "body": "An 8-tray blast freezer has been installed at the main kitchen, "
                 "increasing our frozen output capacity."},
    ],
}

# Branches the operator can open during a demo. Each one is a genuine premises change that
# the policy schedule will not know about.
OPENABLE: dict = {
    "kaki-bukit": {
        "id": "kaki-bukit",
        "label": "Kaki Bukit Central Kitchen",
        "unit": "#05-01",
        "street": "9 Kaki Bukit Road 1",
        "postal": "417818",
        "opened": "August 2026",
        "note": "Second production site opened to serve east-side catering demand.",
        "published": True,
    },
    "jurong": {
        "id": "jurong",
        "label": "Jurong Packing & Despatch",
        "unit": "#02-14",
        "street": "15 Jurong Port Road",
        "postal": "619092",
        "opened": "August 2026",
        "note": "Packing line and despatch bay for west-side wholesale routes.",
        "published": True,
    },
}


# ── state ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    with _LOCK:
        if not STATE_PATH.exists():
            save_state(copy.deepcopy(BASELINE))
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A half-written or hand-edited file must not take the site down mid-demo.
            state = copy.deepcopy(BASELINE)
            save_state(state)
            return state


def save_state(state: dict) -> None:
    with _LOCK:
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def published_locations(state: dict) -> list[dict]:
    return [x for x in state.get("locations", []) if x.get("published")]


# ── rendering ────────────────────────────────────────────────────────────────

CSS = """
:root{--ink:#1a1d21;--muted:#5b6570;--line:#e3e7eb;--bg:#fbfaf8;--card:#fff;
--accent:#8c1d1d;--accent-soft:#fdf3f0;--gold:#b8894a}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI",-apple-system,Roboto,Helvetica,sans-serif;color:var(--ink);
background:var(--bg);line-height:1.65;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1040px;margin:0 auto;padding:0 24px}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}
.bar{display:flex;align-items:center;justify-content:space-between;padding:18px 0}
.brand{display:flex;align-items:baseline;gap:10px}
.brand b{font-size:21px;letter-spacing:-.3px}
.brand span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.12em}
nav a{margin-left:22px;font-size:14px;color:var(--muted);font-weight:600}
nav a.on,nav a:hover{color:var(--accent)}
.hero{background:linear-gradient(160deg,#7d1919,#a83232);color:#fff;padding:64px 0 58px}
.hero h1{font-size:38px;line-height:1.2;letter-spacing:-.8px;max-width:19ch}
.hero p{margin-top:14px;font-size:17px;opacity:.92;max-width:56ch}
.pill{display:inline-block;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);
padding:5px 13px;border-radius:100px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
margin-bottom:18px}
section{padding:48px 0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.14em;color:var(--gold);margin-bottom:8px}
h3{font-size:26px;letter-spacing:-.4px;margin-bottom:18px}
.grid{display:grid;gap:18px}
.g2{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px}
.card h4{font-size:17px;margin-bottom:4px}
.addr{font-size:14px;color:var(--muted);margin-top:6px}
.addr b{color:var(--ink);font-weight:600}
.tag{display:inline-block;font-size:11px;background:var(--accent-soft);color:var(--accent);
border-radius:4px;padding:2px 8px;margin-top:10px;font-weight:600}
.stat{text-align:center;padding:26px 18px}
.stat .n{font-size:34px;font-weight:800;color:var(--accent);letter-spacing:-1px}
.stat .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:2px}
ul.plain{list-style:none}
ul.plain li{padding:9px 0 9px 22px;border-bottom:1px solid var(--line);position:relative;font-size:15px}
ul.plain li:before{content:"";position:absolute;left:0;top:17px;width:7px;height:7px;
border-radius:50%;background:var(--gold)}
ul.plain li:last-child{border-bottom:0}
.news{border-left:3px solid var(--gold);padding:4px 0 4px 18px;margin-bottom:22px}
.news time{font-size:12px;color:var(--muted);letter-spacing:.04em}
.news h4{font-size:18px;margin:3px 0 5px}
footer{border-top:1px solid var(--line);background:var(--card);padding:30px 0;margin-top:20px;
font-size:13px;color:var(--muted)}
.foot-note{margin-top:10px;font-size:12px;color:#96a0aa}
table.op{width:100%;border-collapse:collapse;margin:14px 0}
table.op td,table.op th{border-bottom:1px solid var(--line);padding:10px 8px;text-align:left;font-size:14px}
table.op th{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
button,input[type=submit]{font:inherit;font-size:13px;font-weight:600;padding:8px 14px;
border-radius:7px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--accent);border-color:#e7c9c9}
input[type=number]{font:inherit;padding:8px;border:1px solid var(--line);border-radius:7px;width:90px}
.op-note{background:#fffbea;border:1px solid #f2e3a8;border-radius:8px;padding:14px 16px;
font-size:13.5px;margin-bottom:20px}
"""


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def page(state: dict, title: str, body: str, active: str = "") -> HTMLResponse:
    co = state["company"]
    links = [("/", "Home"), ("/locations", "Locations"), ("/about", "About"),
             ("/news", "News"), ("/contact", "Contact")]
    nav = "".join(
        f'<a href="{h}" class="{"on" if active == h else ""}">{esc(t)}</a>'
        for h, t in links)
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — {esc(co['name'])}</title>
<style>{CSS}</style></head>
<body>
<!-- Demonstration stand-in for the VERIS project. Not a real company. -->
<header><div class="wrap bar">
  <div class="brand"><b>Hock Lee Foods</b><span>Pte Ltd</span></div>
  <nav>{nav}</nav>
</div></header>
{body}
<footer><div class="wrap">
  <div>{esc(co['name'])} &middot; UEN {esc(co['uen'])} &middot;
       Tel {esc(co['phone'])} &middot; {esc(co['email'])}</div>
  <div class="foot-note">Demonstration stand-in site. Not a real business.</div>
</div></footer>
</body></html>""")


def location_card(loc: dict) -> str:
    addr = (f"{loc['unit']}, {loc['street']}, Singapore {loc['postal']}")
    return f"""<div class="card">
  <h4>{esc(loc['label'])}</h4>
  <div class="addr"><b>{esc(addr)}</b></div>
  <div class="addr">{esc(loc.get('note', ''))}</div>
  <span class="tag">Operating since {esc(loc.get('opened', ''))}</span>
</div>"""


# ── app ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Hock Lee Foods Pte Ltd (demo stand-in site)",
              docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def home():
    """The page Discovery fetches. Everything worth finding is reachable from here,
    because the agent fetches the declared URL and does not crawl."""
    st = load_state()
    co, locs = st["company"], published_locations(st)
    body = f"""
<div class="hero"><div class="wrap">
  <span class="pill">Established {esc(co['since'])} &middot; Singapore</span>
  <h1>{esc(co['tagline'])}</h1>
  <p>Prepared meals manufactured in Singapore for food service, catering and
     retail customers, with our own cold-chain distribution.</p>
</div></div>

<section><div class="wrap">
  <div class="grid g3">
    <div class="card stat"><div class="n">{len(locs)}</div>
      <div class="l">Operating locations</div></div>
    <div class="card stat"><div class="n">{esc(st['headcount'])}</div>
      <div class="l">People on the team</div></div>
    <div class="card stat"><div class="n">{2026 - int(co['since'])}</div>
      <div class="l">Years in operation</div></div>
  </div>
</div></section>

<section><div class="wrap">
  <h2>Where we operate</h2><h3>Our locations</h3>
  <div class="grid g2">{"".join(location_card(x) for x in locs)}</div>
</div></section>

<section><div class="wrap">
  <h2>About us</h2><h3>What we do</h3>
  <p>Hock Lee Foods has been serving Singapore's food service industry since
     {esc(co['since'])}. We are a team of {esc(st['headcount'])} people across production,
     cold-chain logistics and administration.</p>
  <ul class="plain" style="margin-top:16px">
    {"".join(f"<li>{esc(a)}</li>" for a in st.get("activities", []))}
  </ul>
</div></section>

<section><div class="wrap">
  <h2>Latest</h2><h3>News from the kitchen</h3>
  {"".join(f'''<div class="news"><time>{esc(n["date"])}</time>
      <h4>{esc(n["title"])}</h4><p>{esc(n["body"])}</p></div>'''
            for n in st.get("news", []))}
</div></section>"""
    return page(st, "Home", body, "/")


@app.get("/locations", response_class=HTMLResponse)
def locations():
    st = load_state()
    locs = published_locations(st)
    body = f"""<section><div class="wrap">
  <h2>Find us</h2><h3>{len(locs)} operating location{"" if len(locs) == 1 else "s"}</h3>
  <div class="grid g2">{"".join(location_card(x) for x in locs)}</div>
</div></section>"""
    return page(st, "Locations", body, "/locations")


@app.get("/about", response_class=HTMLResponse)
def about():
    st = load_state()
    body = f"""<section><div class="wrap">
  <h2>About</h2><h3>A Singapore food manufacturer since {esc(st['company']['since'])}</h3>
  <p>We are a team of {esc(st['headcount'])} people. Our work covers:</p>
  <ul class="plain" style="margin-top:14px">
    {"".join(f"<li>{esc(a)}</li>" for a in st.get("activities", []))}
  </ul>
  <h3 style="margin-top:34px">Our facilities</h3>
  <ul class="plain">
    {"".join(f"<li>{esc(e)}</li>" for e in st.get("equipment", []))}
  </ul>
</div></section>"""
    return page(st, "About", body, "/about")


@app.get("/news", response_class=HTMLResponse)
def news():
    st = load_state()
    body = f"""<section><div class="wrap"><h2>Newsroom</h2><h3>Announcements</h3>
  {"".join(f'''<div class="news"><time>{esc(n["date"])}</time>
      <h4>{esc(n["title"])}</h4><p>{esc(n["body"])}</p></div>'''
            for n in st.get("news", []))}
</div></section>"""
    return page(st, "News", body, "/news")


@app.get("/contact", response_class=HTMLResponse)
def contact():
    st = load_state()
    co = st["company"]
    body = f"""<section><div class="wrap"><h2>Contact</h2><h3>Talk to us</h3>
  <div class="card" style="max-width:460px">
    <div class="addr"><b>{esc(co['name'])}</b></div>
    <div class="addr">UEN {esc(co['uen'])}</div>
    <div class="addr">Tel {esc(co['phone'])}</div>
    <div class="addr">{esc(co['email'])}</div>
  </div>
</div></section>"""
    return page(st, "Contact", body, "/contact")


# ── the operator panel: how a demo changes the business ──────────────────────

@app.get("/operator", response_class=HTMLResponse)
def operator():
    st = load_state()
    rows = ""
    for loc in st.get("locations", []):
        live = loc.get("published")
        rows += f"""<tr>
          <td><b>{esc(loc['label'])}</b><br>
              <span style="color:#5b6570">{esc(loc['unit'])}, {esc(loc['street'])},
              Singapore {esc(loc['postal'])}</span></td>
          <td>{"Published" if live else "Hidden"}</td>
          <td><form method="post" action="/operator/toggle">
              <input type="hidden" name="loc_id" value="{esc(loc['id'])}">
              <button class="{'danger' if live else 'primary'}">
                {"Close this branch" if live else "Open this branch"}</button>
          </form></td></tr>"""

    closed = [k for k, v in OPENABLE.items()
              if not any(x["id"] == v["id"] for x in st.get("locations", []))]
    add = ""
    for key in closed:
        v = OPENABLE[key]
        add += f"""<form method="post" action="/operator/open" style="display:inline">
          <input type="hidden" name="preset" value="{esc(key)}">
          <button class="primary">Open {esc(v['label'])}</button></form> """

    body = f"""<section><div class="wrap">
  <h2>Operator panel</h2><h3>Change the business</h3>
  <div class="op-note"><b>This panel is the demo control, not part of the website.</b>
    Change something here, then re-run Discovery in VERIS. The agent fetches this site
    over HTTP, compares what it reads against the previous look, and reports what is new.
    Nothing tells VERIS that a change happened — it has to notice.</div>

  <table class="op"><thead><tr><th>Location</th><th>On the website</th><th></th></tr>
    </thead><tbody>{rows}</tbody></table>

  {f'<p style="margin:16px 0">{add}</p>' if add else
   '<p style="margin:16px 0;color:#5b6570">All preset branches are already open.</p>'}

  <form method="post" action="/operator/headcount" style="margin-top:22px">
    <label style="font-size:14px">Team size published on the site:
      <input type="number" name="headcount" min="1" max="500"
             value="{esc(st['headcount'])}"></label>
    <button class="primary">Update</button>
  </form>

  <form method="post" action="/operator/reset" style="margin-top:26px">
    <button>Reset the site to its baseline</button>
  </form>
</div></section>"""
    return page(st, "Operator", body, "")


@app.post("/operator/toggle")
def op_toggle(loc_id: str = Form(...)):
    st = load_state()
    for loc in st.get("locations", []):
        if loc["id"] == loc_id:
            loc["published"] = not loc.get("published")
    save_state(st)
    return RedirectResponse("/operator", status_code=303)


@app.post("/operator/open")
def op_open(preset: str = Form(...)):
    st = load_state()
    v = OPENABLE.get(preset)
    if v and not any(x["id"] == v["id"] for x in st.get("locations", [])):
        st.setdefault("locations", []).append(copy.deepcopy(v))
        st.setdefault("news", []).insert(0, {
            "date": "2026-08-11",
            "title": f"{v['label']} now open",
            "body": f"We have opened a new site at {v['unit']}, {v['street']}, "
                    f"Singapore {v['postal']}. {v['note']}",
        })
    save_state(st)
    return RedirectResponse("/operator", status_code=303)


@app.post("/operator/headcount")
def op_headcount(headcount: int = Form(...)):
    st = load_state()
    st["headcount"] = max(1, min(500, int(headcount)))
    save_state(st)
    return RedirectResponse("/operator", status_code=303)


@app.post("/operator/reset")
def op_reset():
    save_state(copy.deepcopy(BASELINE))
    return RedirectResponse("/operator", status_code=303)


@app.get("/api/state")
def api_state():
    """Machine-readable state, for tests and for checking what the site is publishing."""
    st = load_state()
    return JSONResponse({
        "company": st["company"],
        "headcount": st["headcount"],
        "published_locations": published_locations(st),
        "activities": st.get("activities", []),
        "notice": "Demonstration stand-in site for the VERIS project.",
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
