#!/usr/bin/env python3
"""
AI tool status robot.

GitHub runs this file automatically about every 10 minutes. Each run it:
  1. checks every tool listed in tools.json (its website + its official status page, if any)
  2. remembers the last 24 hours of results in data/state.json
  3. rebuilds the whole website into the  site/  folder

You never need to run it yourself. (For developers:
  python update.py               check everything, then build
  python update.py --build-only  build from the last saved results, no checking
  python update.py --demo        build a preview with made-up statuses into preview/)

Uses only the Python standard library, so nothing has to be installed.
"""
import html
import json
import random
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
STATE_FILE = ROOT / "data" / "state.json"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 15          # seconds to wait for a website
SLOW_MS = 8000        # slower than this counts as "Issues"
KEEP_HOURS = 24       # history kept for the uptime figure
WORKERS = 24          # websites checked at the same time
MID_AD_AFTER = 25     # middle ad appears after this many tools

LABEL = {"up": "Up", "issues": "Issues", "down": "Down",
         "maintenance": "Maintenance", "unknown": "Not checked yet"}
SENTENCE = {"up": "up and responding normally", "issues": "having issues",
            "down": "down", "maintenance": "under maintenance"}
HIST_CHAR = {"up": "U", "issues": "I", "down": "D", "maintenance": "M", "unknown": "-"}

LOGO = ('<svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true">'
        '<rect width="32" height="32" rx="8" fill="#4f46e5"/>'
        '<path d="M5 17h6l3-7 4 13 3-6h6" fill="none" stroke="#fff" stroke-width="2.4" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>')


# ----------------------------------------------------------------- helpers
def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tool"


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fmt_ms(ms):
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms} ms"


def fmt_up(up):
    if up is None:
        return "—"
    return "100%" if up >= 100 else f"{up:.1f}%"


def ago_text(since, now):
    if not since:
        return ""
    dt = parse_iso(since)
    if not dt:
        return ""
    mins = int((now - dt).total_seconds() // 60)
    if mins < 1:
        return ""
    if mins < 60:
        return f"since {mins} min ago"
    hours = round(mins / 60)
    return f"since {hours} hour{'s' if hours != 1 else ''} ago"


def load_tools():
    tools = read_json(ROOT / "tools.json", [])
    seen, out = set(), []
    for t in tools:
        if not t.get("name") or not t.get("url"):
            continue
        tid = t.get("id") or slugify(t["name"])
        base, n = tid, 2
        while tid in seen:
            tid = f"{base}-{n}"
            n += 1
        seen.add(tid)
        t["id"] = tid
        t.setdefault("category", "Other")
        t.setdefault("pricing", "Freemium")
        t.setdefault("about", "")
        out.append(t)
    return out


def load_config():
    cfg = read_json(ROOT / "site-config.json", {})
    cfg = {k: (v.strip() if isinstance(v, str) else v) for k, v in cfg.items()}
    cfg.setdefault("site_name", "AI Status Now")
    cfg["site_url"] = (cfg.get("site_url") or "").rstrip("/")
    return cfg


# ----------------------------------------------------------------- checking
SSL_CTX = ssl.create_default_context()


def fetch(url, timeout=TIMEOUT, limit=30000):
    """Return (status_code, headers_lowercase, first_bytes_of_body, milliseconds)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            body = r.read(limit)
            headers = {k.lower(): v for k, v in r.headers.items()}
            return r.status, headers, body, int((time.monotonic() - start) * 1000)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(limit)
        except Exception:
            body = b""
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        return e.code, headers, body, int((time.monotonic() - start) * 1000)


def short_error(e):
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "timed out"
        if isinstance(reason, socket.gaierror):
            return "address not found"
        if isinstance(reason, ssl.SSLError):
            return "secure connection failed"
        if isinstance(reason, ConnectionRefusedError):
            return "connection refused"
        return "could not connect"
    if isinstance(e, ssl.SSLError):
        return "secure connection failed"
    if isinstance(e, ConnectionResetError):
        return "connection reset"
    return "could not connect"


def check_site(url):
    """Is the tool's website answering? Tries twice before giving up."""
    err = ""
    for attempt in range(2):
        try:
            code, headers, body, ms = fetch(url)
            if code < 500:
                # 2xx/3xx/4xx all mean a server answered (403/429 are usually bot protection)
                return {"ok": True, "ms": ms, "code": code}
            if headers.get("cf-mitigated") == "challenge" or b"Just a moment" in body \
                    or b"challenge-platform" in body:
                return {"ok": True, "ms": ms, "code": code}
            err = f"error {code}"
        except Exception as e:  # network trouble of any kind
            err = short_error(e)
        if attempt == 0:
            time.sleep(4)
    return {"ok": False, "ms": None, "code": None, "err": err}


COMPONENT_STATE = {"operational": "up", "degraded_performance": "issues",
                   "partial_outage": "issues", "major_outage": "down",
                   "under_maintenance": "maintenance"}
INDICATOR_STATE = {"none": "up", "minor": "issues", "major": "issues",
                   "critical": "down", "maintenance": "maintenance"}
WORDS = {"issues": "problems", "down": "a major outage", "maintenance": "maintenance"}
ORDER = {"down": 0, "issues": 1, "maintenance": 2, "up": 3}


def fetch_status_page(base):
    """Read an official status page (Atlassian Statuspage-style API). None if unavailable."""
    try:
        code, _, body, _ = fetch(base.rstrip("/") + "/api/v2/summary.json", timeout=12, limit=3_000_000)
        if code != 200:
            return None
        data = json.loads(body.decode("utf-8", "replace"))
        if "status" not in data:
            return None
        return data
    except Exception:
        return None


def official_for(tool, page):
    """Turn status-page data into ('up'|'issues'|'down'|'maintenance', note) for one tool."""
    if not page:
        return None
    wanted = (tool.get("status_component") or "").lower()
    if wanted:
        comps = [c for c in page.get("components", [])
                 if wanted in str(c.get("name", "")).lower() and not c.get("group")]
        states = [COMPONENT_STATE.get(c.get("status")) for c in comps]
        states = [s for s in states if s]
        if not states:
            return None
        worst = min(states, key=lambda s: ORDER[s])
    else:
        worst = INDICATOR_STATE.get(page.get("status", {}).get("indicator"))
        if not worst:
            return None
    note = "" if worst == "up" else f"Company reports {WORDS[worst]}"
    return worst, note


def decide(site, official, prev):
    """Combine our own check with the official page. Returns (status, note, fail_count, source)."""
    fails = prev.get("fail", 0)
    if not site["ok"]:
        fails += 1
        if official and official[0] == "down":
            return "down", official[1], fails, "official"
        if fails >= 2:
            return "down", f"Not responding ({site.get('err') or 'no answer'})", fails, "check"
        return "issues", "Not responding – rechecking", fails, "check"
    if official and official[0] != "up":
        return official[0], official[1], 0, "official"
    if site["ms"] is not None and site["ms"] > SLOW_MS:
        return "issues", "Very slow to respond", 0, "check"
    return "up", "", 0, "official" if official else "check"


def run_checks(tools, state):
    pages = sorted({t["status_page"] for t in tools if t.get("status_page")})
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        page_data = dict(zip(pages, pool.map(fetch_status_page, pages)))
        site_results = list(pool.map(lambda t: check_site(t.get("check_url") or t["url"]), tools))

    now = now_utc()
    times = state.get("times", [])
    old = state.get("tools", {})
    times.append(iso(now))
    new = {}
    for tool, site in zip(tools, site_results):
        prev = old.get(tool["id"], {})
        official = official_for(tool, page_data.get(tool.get("status_page")))
        status, note, fails, source = decide(site, official, prev)
        hist = prev.get("h", "")
        hist = ("-" * max(0, len(times) - 1 - len(hist))) + hist[-(len(times) - 1):] if len(times) > 1 else ""
        hist += HIST_CHAR[status]
        since = prev.get("since") if prev.get("last") == status else iso(now)
        new[tool["id"]] = {
            "last": status, "note": note, "fail": fails, "since": since,
            "ms": site["ms"], "code": site.get("code"), "src": source,
            "official_ok": bool(page_data.get(tool.get("status_page"))), "h": hist,
        }

    # keep only the last 24 hours
    cutoff = now - timedelta(hours=KEEP_HOURS)
    keep_from = 0
    for i, t in enumerate(times):
        dt = parse_iso(t)
        if dt and dt >= cutoff:
            keep_from = i
            break
    else:
        keep_from = len(times) - 1
    times = times[keep_from:]
    for v in new.values():
        v["h"] = v["h"][keep_from:]
    return {"times": times, "tools": new}


def uptime(hist):
    known = [c for c in hist if c != "-"]
    if not known:
        return None
    return round(100 * (1 - known.count("D") / len(known)), 1)


def demo_state(tools):
    rnd = random.Random(7)
    now = now_utc()
    out = {}
    for t in tools:
        r = rnd.random()
        status = "down" if r < 0.015 else "issues" if r < 0.05 else "up"
        note = {"down": "Not responding (timed out)", "issues": "Company reports problems"}.get(status, "")
        hist = "".join("D" if rnd.random() < 0.01 else "U" for _ in range(144))
        out[t["id"]] = {"last": status, "note": note, "fail": 0,
                        "since": iso(now - timedelta(minutes=rnd.randint(12, 90))),
                        "ms": rnd.randint(140, 1900), "src": "check",
                        "official_ok": bool(t.get("status_page")), "h": hist}
    return {"times": [iso(now)], "tools": out}


# ----------------------------------------------------------------- building
def build_status_json(tools, state):
    checked = state["times"][-1] if state.get("times") else None
    summary = {"up": 0, "issues": 0, "down": 0, "maintenance": 0, "unknown": 0}
    out = {}
    for t in tools:
        st = state.get("tools", {}).get(t["id"])
        if not st:
            summary["unknown"] += 1
            out[t["id"]] = {"name": t["name"], "s": "unknown", "ms": None, "up": None, "note": "", "since": None}
            continue
        summary[st["last"]] += 1
        out[t["id"]] = {"name": t["name"], "s": st["last"], "ms": st.get("ms"),
                        "up": uptime(st.get("h", "")), "note": st.get("note", ""),
                        "since": st.get("since")}
    summary["issues_total"] = summary["issues"] + summary["maintenance"]
    return {"checked_at": checked, "summary": {"up": summary["up"], "issues": summary["issues_total"],
                                                "down": summary["down"], "not_checked": summary["unknown"]},
            "tools": out}


def faq_items(status, checked_text, n, tools_by_id):
    def line(tid, name):
        t = status["tools"].get(tid, {})
        s = t.get("s", "unknown")
        if s == "unknown" or not status.get("checked_at"):
            return f"Our first check of {name} has not run yet. "
        return f"At our last check ({checked_text}), {name} was {SENTENCE[s]}. "

    def official(tid, company):
        return (f" We combine our own check with {company}'s official status page."
                if tools_by_id.get(tid, {}).get("status_page") else "")

    return [
        ("Is ChatGPT down right now?",
         line("chatgpt", "ChatGPT") + "Look for ChatGPT at the top of this page: Up means it is answering normally, "
         "Issues or Down means we found a problem or OpenAI is reporting one." + official("chatgpt", "OpenAI")),
        ("Is Claude down right now?",
         line("claude", "Claude") + "Claude's row shows whether claude.ai is answering right now."
         + official("claude", "Anthropic")),
        ("Is Gemini down right now?",
         line("google-gemini", "Google Gemini") + "Gemini's row shows whether gemini.google.com is answering right now."),
        ("How do you check whether an AI tool is down?",
         f"About every 10 minutes our checker visits all {n} tools from the cloud, measures how fast each one answers, "
         "and reads the official status page where a tool publishes one. A tool is marked Down only after it fails "
         "two checks in a row, to avoid false alarms."),
        ("What do Up, Issues and Down mean?",
         "Up means the tool answered normally. Issues means it was very slow, failed one check, or its maker reports "
         "a partial problem or maintenance. Down means it failed two checks in a row or its maker reports a major outage."),
        ("A tool shows Up but it is not working for me. What should I do?",
         "The problem may be on your side or limited to one feature or region. Refresh the page, sign out and back in, "
         "try another browser or switch off extensions, change between Wi-Fi and mobile data, and check the tool's "
         "official status page."),
        ("How do I keep an eye on the AI tools I use?",
         "Tap the star next to any tool to add it to your watchlist. It is saved only in your browser, with no sign-up. "
         "While this page is open it refreshes every few minutes and shows an alert at the top if a tool on your "
         "watchlist has a problem. Use “Copy link to my watchlist” to open the same list on another device."),
        ("Which AI tools do you track, and can I suggest one?",
         f"We track {n} popular AI tools for chat, search, coding, images, video, voice, music, writing, meetings, "
         "design, automation and developer APIs. If a tool is missing, use “Request a tool” and we will "
         "consider adding it."),
    ]


def ad_html(cfg, key, in_table=False):
    client, slot = cfg.get("adsense_client", ""), cfg.get(f"ad_slot_{key}", "")
    if client and slot:
        inner = (f'<span class="ad-label">Advertisement</span>'
                 f'<ins class="adsbygoogle" style="display:block" data-ad-client="{esc(client)}" '
                 f'data-ad-slot="{esc(slot)}" data-ad-format="auto" data-full-width-responsive="true"></ins>'
                 '<script>(adsbygoogle=window.adsbygoogle||[]).push({});</script>')
        block = f'<div class="ad ad-{key}">{inner}</div>'
    elif client:
        return ""  # waiting for ad units; nothing reserved
    else:
        block = f'<div class="ad ad-{key} ad-placeholder" aria-hidden="true">Advertisement space</div>'
    if in_table:
        return f'<tr class="ad-row" aria-hidden="true"><td colspan="7">{block}</td></tr>'
    return block


def head_bits(cfg, page="index.html"):
    url = cfg["site_url"]
    canonical = ""
    if url:
        full = url + "/" + ("" if page == "index.html" else page)
        canonical = (f'<link rel="canonical" href="{esc(full)}">\n'
                     f'<meta property="og:url" content="{esc(full)}">\n'
                     f'<meta property="og:image" content="{esc(url)}/og-image.png">\n'
                     f'<meta name="twitter:image" content="{esc(url)}/og-image.png">')
    ver = ""
    if cfg.get("google_site_verification"):
        ver += f'<meta name="google-site-verification" content="{esc(cfg["google_site_verification"])}">\n'
    if cfg.get("bing_site_verification"):
        ver += f'<meta name="msvalidate.01" content="{esc(cfg["bing_site_verification"])}">\n'
    ga = cfg.get("google_analytics_id", "")
    analytics = ""
    if ga:
        g = esc(ga)
        analytics = (f'<script async src="https://www.googletagmanager.com/gtag/js?id={g}"></script>'
                     "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}"
                     f"gtag('js',new Date());gtag('config','{g}');</script>")
    client = cfg.get("adsense_client", "")
    adsense = ""
    if client:
        c = esc(client)
        adsense = (f'<meta name="google-adsense-account" content="{c}">\n'
                   f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={c}" '
                   'crossorigin="anonymous"></script>')
    return canonical, ver, analytics, adsense


def request_link(cfg):
    if cfg.get("request_form_url"):
        return esc(cfg["request_form_url"]), ' target="_blank" rel="noopener"'
    if cfg.get("contact_email"):
        return esc("mailto:" + cfg["contact_email"] + "?subject=Please track this AI tool"), ""
    return "#request", ""


def fill(template, values):
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def build_site(tools, cfg, state, out=SITE, demo=False):
    out.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "static").iterdir():
        if f.is_file():
            shutil.copy2(f, out / f.name)

    now = now_utc()
    status = build_status_json(tools, state)
    st_tools = status["tools"]
    checked_dt = parse_iso(status["checked_at"]) if status["checked_at"] else None
    checked_text = checked_dt.strftime("%d %b %Y, %H:%M UTC") if checked_dt else \
        "not yet (the first check runs within 10 minutes of going live)"
    checked_iso = status["checked_at"] or ""
    n = len(tools)
    name = cfg["site_name"]
    url = cfg["site_url"]
    by_id = {t["id"]: t for t in tools}

    # popular order: featured first, then file order
    ordered = [t for t in tools if t.get("featured")] + [t for t in tools if not t.get("featured")]
    order_of = {t["id"]: i for i, t in enumerate(ordered)}

    # ---- table rows
    row_html = []
    for i, t in enumerate(ordered):
        s = st_tools[t["id"]]
        stt = state.get("tools", {}).get(t["id"], {})
        note = " · ".join(x for x in [s["note"], ago_text(s["since"], now) if s["s"] not in ("up", "unknown") else ""] if x)
        sp = ""
        if t.get("status_page") and stt.get("official_ok"):
            sp = f' <a class="sp" href="{esc(t["status_page"])}" target="_blank" rel="nofollow noopener">Official status</a>'
        q = " ".join([t["name"], t["about"], t["category"], t["pricing"], t["url"]]).lower()
        row_html.append(
            f'<tr id="{esc(t["id"])}" data-id="{esc(t["id"])}" data-name="{esc(t["name"])}" '
            f'data-cat="{esc(t["category"])}" data-price="{esc(t["pricing"])}" data-status="{s["s"]}" '
            f'data-order="{order_of[t["id"]]}" data-ms="{"" if s["ms"] is None else s["ms"]}" '
            f'data-up="{"" if s["up"] is None else s["up"]}" data-q="{esc(q)}">'
            f'<td class="c-star"><button type="button" class="star" aria-pressed="false" '
            f'aria-label="Add {esc(t["name"])} to watchlist">&#9734;</button></td>'
            f'<td class="c-name"><a href="{esc(t["url"])}" target="_blank" rel="nofollow noopener">{esc(t["name"])}</a>'
            f'<span class="about">{esc(t["about"])}</span>'
            f'<span class="meta">{esc(t["category"])} · {esc(t["pricing"])}</span></td>'
            f'<td class="c-cat">{esc(t["category"])}</td>'
            f'<td class="c-price">{esc(t["pricing"])}</td>'
            f'<td class="c-status"><span class="badge {s["s"]}">{LABEL[s["s"]]}</span>'
            f'<span class="note">{esc(note)}</span>{sp}</td>'
            f'<td class="c-resp">{fmt_ms(s["ms"])}</td>'
            f'<td class="c-uptime">{fmt_up(s["up"])}</td></tr>')
        if i == MID_AD_AFTER - 1:
            row_html.append(ad_html(cfg, "middle", in_table=True))
    if len(ordered) < MID_AD_AFTER:
        row_html.append(ad_html(cfg, "middle", in_table=True))

    # ---- featured chips, summary, categories
    featured = "".join(
        f'<li><a href="#{esc(t["id"])}" data-fid="{esc(t["id"])}"><span class="dot {st_tools[t["id"]]["s"]}"></span>'
        f'{esc(t["name"])}<span class="ft {st_tools[t["id"]]["s"]}">{LABEL[st_tools[t["id"]]["s"]]}</span></a></li>'
        for t in tools if t.get("featured"))
    sm = status["summary"]
    summary = (f'<span class="s up"><b>{sm["up"]}</b> up</span>'
               f'<span class="s issues"><b>{sm["issues"]}</b> with issues</span>'
               f'<span class="s down"><b>{sm["down"]}</b> down</span>')
    cats = []
    for t in tools:
        if t["category"] not in cats:
            cats.append(t["category"])
    cat_options = "".join(f"<option>{esc(c)}</option>" for c in cats)

    # ---- FAQ + structured data
    faqs = faq_items(status, checked_text, n, by_id)
    faq_html = "\n".join(f"  <h3>{esc(q)}</h3>\n  <p>{esc(a)}</p>" for q, a in faqs)
    title = f"Is ChatGPT Down? Live Status of {n} AI Tools – {name}"
    desc = (f"Is ChatGPT, Claude, Gemini or Grok down? See the live status of {n} AI tools, "
            "checked every 10 minutes. Search, filter and keep a free watchlist.")
    home = (url + "/") if url else ""
    graph = [
        {"@type": "WebSite", "@id": home + "#website", "name": name, "description": desc,
         **({"url": home} if home else {})},
        {"@type": "WebPage", "@id": home + "#webpage", "name": title, "description": desc,
         "isPartOf": {"@id": home + "#website"}, "inLanguage": "en",
         **({"url": home} if home else {}), **({"dateModified": checked_iso} if checked_iso else {})},
        {"@type": "ItemList", "name": f"Live status of {n} AI tools", "numberOfItems": n,
         "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t["name"], "url": t["url"]}
                             for i, t in enumerate(ordered)]},
        {"@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": q,
                                             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faqs]},
    ]
    jsonld = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False,
                        separators=(",", ":")).replace("</", "<\\/")

    canonical, ver, analytics, adsense = head_bits(cfg)
    req_href, req_target = request_link(cfg)
    contact = cfg.get("contact_email", "")
    contact_link = f' &middot; <a href="mailto:{esc(contact)}">Contact</a>' if contact else ""
    demo_banner = ('<div class="demo">Preview with made-up statuses. The live site shows real checks.</div>'
                   if demo else "")

    page = fill((ROOT / "templates" / "index.html").read_text(encoding="utf-8"), {
        "TITLE": esc(title), "DESCRIPTION": esc(desc), "SITE_NAME": esc(name),
        "CANONICAL_TAGS": canonical, "VERIFICATION": ver, "ANALYTICS": analytics, "ADSENSE_HEAD": adsense,
        "JSONLD": jsonld, "DEMO_BANNER": demo_banner, "LOGO": LOGO,
        "REQUEST_HREF": req_href, "REQUEST_TARGET": req_target, "TOOL_COUNT": str(n),
        "CHECKED_ISO": esc(checked_iso), "CHECKED_TEXT": esc(checked_text),
        "SUMMARY": summary, "FEATURED": featured, "CATEGORY_OPTIONS": cat_options,
        "AD_TOP": ad_html(cfg, "top"), "AD_BOTTOM": ad_html(cfg, "bottom"),
        "ROWS": "\n".join(row_html), "FAQ_HTML": faq_html,
        "YEAR": str(now.year), "CONTACT_LINK": contact_link,
    })
    write_text(out / "index.html", page)

    # ---- privacy page
    p_canon, _, p_analytics, p_adsense = head_bits(cfg, "privacy.html")
    contact_sentence = (f'Questions? Email us at <a href="mailto:{esc(contact)}">{esc(contact)}</a>.'
                        if contact else "Questions? Use the request form on the home page to reach us.")
    privacy = fill((ROOT / "templates" / "privacy.html").read_text(encoding="utf-8"), {
        "SITE_NAME": esc(name), "CANONICAL_TAGS": p_canon, "ANALYTICS": p_analytics,
        "ADSENSE_HEAD": p_adsense, "LOGO": LOGO, "UPDATED_DATE": "5 October 2026",
        "CONTACT_SENTENCE": contact_sentence,
    })
    write_text(out / "privacy.html", privacy)

    # ---- machine-readable files
    write_text(out / "status.json", json.dumps(status, ensure_ascii=False, separators=(",", ":")))

    bots = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "Claude-User",
            "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot-Extended", "Bingbot", "Googlebot"]
    robots = "User-agent: *\nAllow: /\n\n# AI search engines and assistants are welcome here\n"
    robots += "".join(f"User-agent: {b}\n" for b in bots) + "Allow: /\n"
    if url:
        robots += f"\nSitemap: {url}/sitemap.xml\n"
    write_text(out / "robots.txt", robots)

    if url:
        lastmod = checked_iso or iso(now)
        sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                   f"  <url><loc>{esc(url)}/</loc><lastmod>{lastmod}</lastmod>"
                   "<changefreq>always</changefreq><priority>1.0</priority></url>\n"
                   f"  <url><loc>{esc(url)}/privacy.html</loc><changefreq>yearly</changefreq>"
                   "<priority>0.2</priority></url>\n</urlset>\n")
        write_text(out / "sitemap.xml", sitemap)

    link = (lambda p: f"{url}/{p}") if url else (lambda p: p)
    llms = [f"# {name}", "",
            f"> Live up/down status of {n} popular AI tools (ChatGPT, Claude, Gemini, Grok, Copilot and more), "
            "checked about every 10 minutes from the cloud and combined with official status pages.", "",
            f"Last checked: {checked_text}", "",
            "## Data", f"- [Live status board]({link('')}): the human-readable page",
            f"- [Live status JSON]({link('status.json')}): machine-readable status for every tool "
            "(s = up | issues | down | maintenance | unknown, ms = response time, up = 24h uptime %)", "",
            "## How status is decided",
            "- Down: the tool failed two checks in a row, or its maker reports a major outage.",
            "- Issues: very slow, failed one check, or its maker reports a partial problem or maintenance.",
            "- Up: answered normally.", "",
            "## Current status"]
    for t in ordered:
        s = st_tools[t["id"]]
        extra = f" ({s['note']})" if s["note"] else ""
        llms.append(f"- {t['name']} ({t['category']}): {LABEL[s['s']]}{extra} — {t['url']}")
    write_text(out / "llms.txt", "\n".join(llms) + "\n")

    client = cfg.get("adsense_client", "")
    if client.startswith("ca-pub-"):
        write_text(out / "ads.txt", f"google.com, {client[3:]}, DIRECT, f08c47fec0942fa0\n")

    if url and "github.io" not in url:
        write_text(out / "CNAME", re.sub(r"^https?://", "", url).split("/")[0] + "\n")

    write_text(out / ".nojekyll", "")
    return status


# ----------------------------------------------------------------- main
def main():
    args = set(sys.argv[1:])
    tools = load_tools()
    cfg = load_config()
    if "--demo" in args:
        build_site(tools, cfg, demo_state(tools), out=ROOT / "preview", demo=True)
        print(f"Demo preview written to preview/ ({len(tools)} tools)")
        return
    state = read_json(STATE_FILE, {"times": [], "tools": {}})
    if "--build-only" not in args:
        started = time.monotonic()
        state = run_checks(tools, state)
        write_text(STATE_FILE, json.dumps(state, separators=(",", ":")))
        print(f"Checked {len(tools)} tools in {time.monotonic() - started:.0f}s")
    status = build_site(tools, cfg, state)
    print("Summary:", status["summary"])


if __name__ == "__main__":
    main()
