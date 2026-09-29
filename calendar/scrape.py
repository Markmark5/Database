#!/usr/bin/env python3
"""
Financial-calendar scraper for Mark's Database.

Reads calendar/sources.csv, fetches every company calendar page, extracts
upcoming RESULTS, TRADING UPDATES and AGMs only (no dividends, CMDs,
conferences, year-ends), and writes them to calendar/pending_events.json
for manual approval. It never writes to the live calendar.

Outputs (all in calendar/):
  pending_events.json   events awaiting approval (new / changed)
  scrape_report.json    per-URL outcome
  scrape_report.md      readable summary of clean vs failed reads
Reads (if present):
  approved_events.json  events already approved (skipped unless changed)
  rejected.json         ids you rejected (never re-staged)
"""
import asyncio, csv, hashlib, json, os, re, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
TODAY = datetime.now(timezone.utc).date()
HORIZON = TODAY + timedelta(days=550)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
HTTP_CONCURRENCY = 12
BROWSER_CONCURRENCY = 4

# ---------------------------------------------------------------- dates
MONTHS = {m: i for i, names in enumerate([
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"),
    ("december", "dec")], start=1) for m in names}
MON = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"
RE_DMY = re.compile(rf"\b(\d{{1,2}})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?{MON}\.?,?(?:\s+'?(\d{{4}}|\d{{2}})(?![\d:]))?\b", re.I)
RE_MDY = re.compile(rf"\b{MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b,?(?:\s+'?(\d{{4}}|\d{{2}})(?![\d:]))?", re.I)
RE_NUM = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})(?![\d.])")
RE_ISO = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
RE_WIN = re.compile(rf"\b(early|beginning of|mid|middle of|late|end of|towards the end of)?[\s-]*{MON}\.?\s+(\d{{4}})\b", re.I)
RE_YEAR_LINE = re.compile(r"^(20\d\d)$")
RE_DATEISH = re.compile(rf"\b20\d\d-\d\d-\d\d\b|\b\d{{1,2}}\s*(?:st|nd|rd|th)?\s+{MON}\b|\b{MON}\s+20\d\d\b", re.I)
RE_PERIOD_REF = re.compile(r"\b(to|ending|ended|end(?:ed)? on|as at|until|through|period)\s*$", re.I)

WINDOWS = {"early": (1, 10), "beginning of": (1, 10), "mid": (11, 20), "middle of": (11, 20),
           "late": (21, 31), "end of": (21, 31), "towards the end of": (21, 31)}
WIN_LABEL = {"early": "Early", "beginning of": "Early", "mid": "Mid", "middle of": "Mid",
             "late": "Late", "end of": "Late", "towards the end of": "Late"}

def last_day(y, m):
    return ((date(y, m % 12 + 1, 1) if m < 12 else date(y + 1, 1, 1)) - timedelta(days=1)).day

def safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None

# ---------------------------------------------------------------- events
RE_AGM = re.compile(r"annual general meeting|\bagm\b", re.I)
RE_RES = re.compile(r"results|interim report|half[- ]?year(?:ly)? report|year[- ]end report|\binterims\b|\bprelim|"
                    r"\binterim (?:presentation|announcement|statement)", re.I)
RE_TU = re.compile(r"trading (?:update|statement|report)|pre[- ]close|post[- ]close|business update|"
                   r"interim management statement|\bq[1-4]\b|\bquarter", re.I)
# never kept, even if the line also says "results" (dividends, CMDs, conferences, year-ends)
RE_EXCLUDE = re.compile(r"dividend|ex[- ]div|record date|payment date|capital markets?|investor day|"
                        r"conference(?! call)|seminar|webinar|site visit|annual report", re.I)
RE_PROV = re.compile(r"provisional|\btbc\b|to be confirmed|indicative|subject to change|expected|\(p\)", re.I)
RE_UPCOMING = re.compile(r"\b(upcoming|forthcoming|future events|key dates|next events?)\b", re.I)
RE_PAST = re.compile(r"\b(past events|previous events|previous dates|past dates|historical|recent past events|archive)\b", re.I)

def classify(text):
    """Return our event type, or None if not a results / TU / AGM line."""
    t = text.lower()
    if RE_EXCLUDE.search(t) and not re.search(r"results|trading|\bagm\b|annual general", t):
        return None
    if re.search(r"capital markets?|investor day|conference(?! call)|seminar|webinar|site visit", t):
        return None
    if re.search(r"notice of (?:the )?(?:agm|annual general)|agm notice|publication|posting of", t):
        return None                      # AGM notice / annual report posting, not the event
    if RE_AGM.search(t):
        return "AGM"
    strong_tu = re.search(r"trading (?:update|statement|report)|pre[- ]close|post[- ]close|business update", t)
    if RE_RES.search(t) and not strong_tu:
        if re.search(r"half|interim|\bh1\b|\b1h(?:\d\d)?\b|\bhy\b|six months", t):
            return "H1 results"
        if re.search(r"full[- ]year|final|prelim|annual results|year[- ]end|\bfy|twelve months|12 months", t):
            return "FY results"
        return "Other"
    if RE_TU.search(t):
        if re.search(r"\bq1\b|first quarter", t):
            return "Q1"
        if re.search(r"\bq3\b|third quarter|nine months|9 months", t):
            return "Q3"
        if re.search(r"year[- ]end|full[- ]year|\bfy\b|pre[- ]close", t) and not re.search(r"half", t):
            return "Pre-FY TU"
        if re.search(r"half|\bh1\b", t):
            return "Pre-H1 TU"
        return "Trading update"
    return None

# ---------------------------------------------------------------- page → lines
def page_lines(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    # ASP.NET pages wrap everything in <form>, and some sites put "cookie" in the
    # <body> class - so only strip nav/footer/header/cookie blocks that are small
    for el in soup.find_all(["nav", "footer", "header"]) + \
              soup.find_all(attrs={"class": re.compile(r"cookie|consent|gdpr", re.I)}) + \
              soup.find_all(attrs={"id": re.compile(r"cookie|consent|gdpr", re.I)}):
        if el.decomposed or el.name in ("html", "body", "main", "form"):
            continue
        if len(el.get_text(" ", strip=True)) < 2500 and not RE_DATEISH.search(el.get_text(" ")):
            el.decompose()
    for tr in soup.find_all("tr"):   # keep a table row on one line
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        tr.replace_with(soup.new_string("\n" + " | ".join(c for c in cells if c) + "\n"))
    raw = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").split("\n")]
    lines = [l for l in raw if l]
    # merge split widget dates: "18" + "Nov", "Oct" + "15th", "Mar" + "2027"
    out, i = [], 0
    while i < len(lines):
        a = lines[i]; b = lines[i + 1] if i + 1 < len(lines) else ""
        if re.fullmatch(r"\d{1,2}", a) and re.fullmatch(MON + r"\.?(\s+'?(\d{4}|\d{2}))?", b, re.I):
            out.append(f"{a} {b}"); i += 2; continue
        if re.fullmatch(MON + r"\.?", a, re.I) and re.fullmatch(r"\d{1,2}\s*(st|nd|rd|th)?", b, re.I):
            out.append(f"{re.sub(r'[^0-9]', '', b)} {a}"); i += 2; continue
        if re.fullmatch(MON + r"\.?", a, re.I) and re.fullmatch(r"\d{4}", b):
            out.append(f"{a} {b}"); i += 2; continue
        out.append(a); i += 1
    return out

# ---------------------------------------------------------------- extraction
def find_dates(line):
    """Yield (from, to, label, yearless, span) for each date in a line."""
    taken = []
    def free(s, e):
        return all(e <= a or s >= b for a, b in taken)
    for rx, order in ((RE_ISO, "iso"), (RE_NUM, "num"), (RE_DMY, "dmy"), (RE_MDY, "mdy")):
        for m in rx.finditer(line):
            if not free(*m.span()):
                continue
            if order == "iso":
                y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
            elif order == "num":
                d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            elif order == "dmy":
                d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3)
            else:
                mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
            taken.append(m.span())
            if not 1 <= mo <= 12:
                continue
            if y is not None:
                y = int(y); y = 2000 + y if y < 100 else y
                if not 2020 <= y <= 2035:        # old or odd year ("22 Oct 2019"): drop, never roll forward
                    continue
            yield ("exact", d, mo, y, m.span())
    for m in RE_WIN.finditer(line):
        if not free(*m.span()):
            continue
        q = (m.group(1) or "").lower()
        yield ("window", q, MONTHS[m.group(2).lower()], int(m.group(3)), m.span())
        taken.append(m.span())

RE_BOILER = re.compile(r"^(?:(?:\d{1,2}[:.]\d{2}\s*(?:am|pm)?(?:\s*(?:gmt|bst|uk|utc|cet))?|"
                       r"add to [a-z ]*calendar|add to microsoft outlook|date|dates|title|event|events|time|location|venue|details|type|description|"
                       r"reminder alert|add to (?:my )?calendar|add to outlook|outlook link|outlook|ical|google|"
                       r"google calendar|yahoo calendar|download|days before event|webcast|more info|read more|"
                       r"view|register|add event|save|share)\s*\|?\s*)+$", re.I)

def is_year_line(lines, j):
    return 0 <= j < len(lines) and bool(RE_YEAR_LINE.match(lines[j]))

def in_tab_bar(lines, j):
    """Year lines stacked together (2026 / 2025 / 2024) are a tab bar, not a heading."""
    return is_year_line(lines, j - 1) or is_year_line(lines, j + 1)

def nearby_year(lines, i):
    """Year for a yearless date: an adjacent year line ("14 Oct" / "2026"), else the
    nearest year heading above, else a year line a little way below."""
    ok = lambda j: is_year_line(lines, j) and not in_tab_bar(lines, j)
    for j in [i + 1, i - 1] + list(range(i - 2, i - 16, -1)) + list(range(i + 2, i + 7)):
        if ok(j):
            return int(lines[j])
    return None

def residue(line):
    for *_, (a, b) in find_dates(line):
        line = line[:a] + " " * (b - a) + line[b:]
    return re.sub(r"\b20\d\d\b", " ", line)

def has_words(text):
    return len(re.findall(r"[A-Za-z]", residue(text))) >= 3

def event_dates(line):
    """Dates in a line that are not period references ("quarter ending 30 Sept")."""
    return [d for d in find_dates(line) if not RE_PERIOD_REF.search(line[max(0, d[4][0] - 30):d[4][0]])]

def is_label(line):
    return has_words(line) and not RE_BOILER.match(line.strip())

def _md(line):
    return {(d[2], d[1]) for d in event_dates(line)}

def _scan(lines, i, js):
    """First real label line in js; stop at the next dated row. Repeats of this row's
    own date ("Oct 5 2026" / "Oct 5" / "2026") are skipped, not treated as a new row."""
    own = _md(lines[i])
    for j in js:
        if event_dates(lines[j]):
            if not has_words(lines[j]) and _md(lines[j]) <= own:
                continue
            return None            # reached the next dated row first
        if is_label(lines[j]):
            return lines[j]
    return None

def label_below(lines, i):
    return _scan(lines, i, range(i + 1, min(i + 6, len(lines))))

def label_above(lines, i):
    return _scan(lines, i, range(i - 1, max(i - 5, -1), -1))

def page_layout(lines):
    """For date-only rows: is the event label above or below the date? Majority vote."""
    up = down = 0
    for i, l in enumerate(lines):
        if not event_dates(l) or is_label(l):
            continue
        a, b = label_above(lines, i), label_below(lines, i)
        ca, cb = bool(a and classify(a)), bool(b and classify(b))
        if ca and not cb and a is not None:
            up += 1
        elif cb and not ca and b is not None:
            down += 1
    return "above" if up > down else "below"

def neighbour_label(lines, i, layout="below"):
    first, second = (label_above, label_below) if layout == "above" else (label_below, label_above)
    text = first(lines, i) or second(lines, i) or ""
    return text, (classify(text) if text else None)

def extract(lines):
    events, zone = [], None
    layout = page_layout(lines)
    # footnote such as "*subject to change" / "All future dates are indicative" covers the whole table
    page_prov = any(len(l) < 150 and RE_PROV.search(l) and (l.startswith("*") or re.search(r"\bdates?\b", l, re.I))
                    for l in lines)
    tab_year, tab_last = None, None      # year tab bar (2026 / 2025 / 2024) above a yearless table
    for i, line in enumerate(lines):
        if len(line) < 60 and RE_PAST.search(line):
            zone = "past"
        elif len(line) < 60 and RE_UPCOMING.search(line):
            zone = "upcoming"
        if is_year_line(lines, i) and is_year_line(lines, i + 1) and not is_year_line(lines, i - 1):
            tab_year, tab_last = int(line), None
        # period references ("nine months to 30 September") are skipped by event_dates
        for kind, a, mo, y, span in event_dates(line):
            conf = "confirmed"
            if y is None:  # yearless: needs a nearby year line, a year tab bar or an upcoming heading
                y = nearby_year(lines, i)
                if y is None and tab_year and kind == "exact":
                    # first tab's table only: rows run newest-first, so stop when the order breaks
                    if tab_last is None or (mo, a) <= tab_last:
                        y, tab_last = tab_year, (mo, a)
                    else:
                        tab_year = None
                if y is None:
                    if zone != "upcoming":
                        continue
                    y = TODAY.year if (TODAY.month, TODAY.day) <= (mo, a) else TODAY.year + 1
                conf = "implied"
            if kind == "exact":
                d0 = safe_date(y, mo, a)
                if not d0:
                    continue
                d1, label = d0, ""
            else:
                lo, hi = WINDOWS.get(a, (1, 31))
                d0 = safe_date(y, mo, lo); d1 = safe_date(y, mo, min(hi, last_day(y, mo)))
                label = f"{WIN_LABEL.get(a, '')} {d0.strftime('%b %Y')}".strip()
                conf = "provisional"
            if d1 < TODAY or d0 > HORIZON:
                continue
            # event text: same line minus the date, else neighbours
            text = (line[:span[0]] + " " + line[span[1]:])
            text = re.sub(r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b,?", " ", text, flags=re.I).strip(" |-–:,")
            if has_words(text):          # label on the same line: judge that alone
                etype = classify(text)
            else:                        # label on a neighbouring line
                text, etype = neighbour_label(lines, i, layout)
            if not etype:
                continue
            if conf == "confirmed" and (page_prov or RE_PROV.search(line + " " + text)):
                conf = "provisional"
            text = " | ".join(c for c in (c.strip() for c in text.split("|"))
                              if c and not RE_BOILER.match(c) and not re.match(r"(download|google|yahoo|outlook)\b", c, re.I))
            events.append({"type": etype, "from": d0.isoformat(), "to": d1.isoformat(),
                           "label": label, "conf": conf,
                           "event": re.sub(r"\s+", " ", text)[:90],
                           "snippet": line[:160]})
    seen, out = set(), []
    for e in events:
        k = (e["type"], e["from"], e["to"])
        if k not in seen:
            seen.add(k); out.append(e)
    return out

# ---------------------------------------------------------------- fetching
async def fetch_http(client, url):
    for attempt in range(2):
        try:
            r = await client.get(url)
            if r.status_code >= 500 and attempt == 0:
                await asyncio.sleep(3); continue
            return r.status_code, r.text
        except Exception as ex:
            if attempt == 1:
                return None, f"{type(ex).__name__}: {ex}"
            await asyncio.sleep(3)

async def fetch_browser(browser, url):
    page = await browser.new_page(user_agent=UA, locale="en-GB")
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        for label in ("Accept all", "Accept All", "Accept", "I agree", "Allow all", "Agree"):
            try:
                btn = page.get_by_role("button", name=label, exact=False)
                if await btn.count():
                    await btn.first.click(timeout=2000); await page.wait_for_timeout(1500); break
            except Exception:
                pass
        await page.wait_for_timeout(2000)
        html = await page.content()
        for fr in page.frames[1:]:        # Investis / Euroland widgets live in iframes
            try:
                html += "\n" + await fr.content()
            except Exception:
                pass
        return (resp.status if resp else None), html
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"
    finally:
        await page.close()

async def browser_pass(p, sources, results, idxs, method, **launch):
    proxy = os.environ.get("HTTPS_PROXY")   # only set when run behind a proxy
    try:
        browser = await p.chromium.launch(proxy={"server": proxy} if proxy else None, **launch)
    except Exception as ex:
        print(f"{method}: launch failed - {ex}", file=sys.stderr)
        return
    bsem = asyncio.Semaphore(BROWSER_CONCURRENCY)
    async def run(i):
        async with bsem:
            src = sources[i]
            u = src["alt_url"] or src["url"]
            status, html = await fetch_browser(browser, u)
            lines = page_lines(html) if status and status < 400 else []
            if is_challenge(lines):
                status, lines = 403, []
            n = count_dates(lines)
            old = results[i]
            better = (n > old["n_dates"] or (old["status"] or 0) >= 400 or old["status"] is None
                      or len(" ".join(lines)) > len(" ".join(old["lines"])) + 500)
            if better:
                results[i] = {"fetched": u, "method": method, "status": status,
                              "html": html, "lines": lines, "n_dates": n}
    await asyncio.gather(*(run(i) for i in idxs))
    await browser.close()

RE_CHALLENGE = re.compile(r"attention required!? \| cloudflare|sorry, you have been blocked|^just a moment\.\.\.$|"
                          r"checking your browser|access denied|request unsuccessful\. incapsula", re.I)

def is_challenge(lines):
    """Bot-protection page served with a 200 (Cloudflare, Incapsula, Akamai)."""
    return len(" ".join(lines)) < 3000 and any(RE_CHALLENGE.search(l) for l in lines[:40])

def needs_browser(status, html, lines, n_dates):
    if status is None or status in (401, 403, 429) or status >= 500:
        return True
    text = " ".join(lines).lower()
    return len(text) < 1500 or "enable javascript" in text or "javascript is disabled" in text or n_dates == 0

def count_dates(lines):
    return sum(1 for l in lines for _ in find_dates(l))

async def process(src, client, sem):
    async with sem:
        urls = [u for u in (src["alt_url"], src["url"]) if u]
        best = None
        for u in urls:
            status, html = await fetch_http(client, u)
            lines = page_lines(html) if status and status < 400 else []
            if is_challenge(lines):
                status, lines = 403, []
            rec = {"fetched": u, "method": "http", "status": status, "html": html, "lines": lines,
                   "n_dates": count_dates(lines)}
            if best is None or rec["n_dates"] > best["n_dates"]:
                best = rec
            if rec["n_dates"]:
                break
        return best

# ---------------------------------------------------------------- main
def load_json(name, default):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else default

def event_id(url, e):
    return hashlib.sha1(f"{url}|{e['type']}|{e['from']}|{e['to']}".encode()).hexdigest()[:12]

async def main():
    import shutil
    shutil.rmtree(HERE / "debug", ignore_errors=True)
    sources = [r for r in csv.DictReader(open(HERE / "sources.csv")) if r.get("active", "1") != "0"]
    approved = load_json("approved_events.json", [])
    rejected = set(load_json("rejected.json", []))

    sem = asyncio.Semaphore(HTTP_CONCURRENCY)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        results = await asyncio.gather(*(process(s, client, sem) for s in sources))

    # browser fallback for JS / blocked / empty pages: bundled Chromium first, then
    # real Google Chrome (installed on GitHub runners) for whatever is still blocked
    retry = [i for i, r in enumerate(results)
             if needs_browser(r["status"], r["html"], r["lines"], r["n_dates"])]
    if retry:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                await browser_pass(p, sources, results, retry, "browser",
                                   args=["--disable-blink-features=AutomationControlled", "--disable-http2"])
                still = [i for i in retry if results[i]["status"] is None or results[i]["status"] in (401, 403, 429)]
                if still:
                    await browser_pass(p, sources, results, still, "chrome", channel="chrome",
                                       args=["--disable-blink-features=AutomationControlled"])
        except ImportError:
            print("playwright not installed - browser fallback skipped", file=sys.stderr)

    pending, report = [], []
    approved_keys = {(a.get("url"), a.get("type"), a.get("from"), a.get("to")) for a in approved}
    for src, r in zip(sources, results):
        events = extract(r["lines"]) if r["lines"] else []
        st = r["status"]
        if st in (401, 403, 429):
            outcome = "BLOCKED"
        elif st is None or st >= 400:
            outcome = "ERROR"
        elif events:
            outcome = "CLEAN"
        elif len(" ".join(r["lines"])) < 1500:
            outcome = "UNREADABLE"
        else:
            outcome = "EMPTY"
        kept = 0
        for e in events:
            key = (src["url"], e["type"], e["from"], e["to"])
            eid = event_id(src["url"], e)
            if key in approved_keys or eid in rejected:
                continue
            prior = [a for a in approved if a.get("url") == src["url"] and a.get("type") == e["type"]
                     and a.get("to", "") >= TODAY.isoformat()]
            e.update({"id": eid, "epic": src["epic"], "company": src["company"], "url": src["url"],
                      "src": "Web", "status": "changed" if prior else "new",
                      "was": f"{prior[0]['from']}" if prior else "",
                      "found": TODAY.isoformat()})
            pending.append(e); kept += 1
        if outcome != "CLEAN":
            dbg = HERE / "debug"; dbg.mkdir(exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9]+", "_", src["company"])[:40]
            (dbg / f"{outcome}_{name}.txt").write_text(
                f"{src['url']}\nhttp {st} via {r['method']}\n\n" +
                ("\n".join(r["lines"]) if r["lines"] else str(r["html"])[:3000]))
        report.append({"epic": src["epic"], "company": src["company"], "url": src["url"],
                       "fetched": r["fetched"], "method": r["method"], "http": st,
                       "text_chars": len(" ".join(r["lines"])), "dates_on_page": r["n_dates"],
                       "events_found": len(events), "events_staged": kept, "outcome": outcome,
                       "error": r["html"][:200] if st is None else ""})

    # approved events whose source failed tonight
    failed = {x["url"] for x in report if x["outcome"] in ("BLOCKED", "ERROR", "UNREADABLE")}
    unverified = [a for a in approved if a.get("url") in failed and a.get("to", "") >= TODAY.isoformat()]

    pending.sort(key=lambda e: (e["from"], e["company"]))
    (HERE / "pending_events.json").write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "count": len(pending), "unverified": unverified, "events": pending}, indent=1))
    (HERE / "scrape_report.json").write_text(json.dumps(report, indent=1))
    write_md(report, pending, unverified)
    tally = {k: sum(1 for x in report if x["outcome"] == k)
             for k in ("CLEAN", "EMPTY", "UNREADABLE", "BLOCKED", "ERROR")}
    print(f"{len(sources)} sources | {tally} | {len(pending)} events staged")

def write_md(report, pending, unverified):
    order = ["CLEAN", "EMPTY", "UNREADABLE", "BLOCKED", "ERROR"]
    note = {"CLEAN": "read OK, upcoming events found",
            "EMPTY": "read OK, no upcoming results/TU/AGM on page (may be genuine)",
            "UNREADABLE": "page loaded but little text (JS-rendered even in browser)",
            "BLOCKED": "401/403/429 - bot protection or login",
            "ERROR": "timeout, DNS or server error"}
    out = [f"# Calendar scrape - {TODAY.isoformat()}", "",
           f"**{len(report)} sources · {len(pending)} events staged for approval**", "",
           "| Outcome | Count | Meaning |", "|---|---|---|"]
    for k in order:
        out.append(f"| {k} | {sum(1 for x in report if x['outcome'] == k)} | {note[k]} |")
    for k in order[1:]:
        rows = [x for x in report if x["outcome"] == k]
        if rows:
            out += ["", f"## {k}", ""] + [f"- {x['company']} ({x['epic'] or '?'}) - "
                                          f"http {x['http']}, {x['method']} - {x['url']}" for x in rows]
    if unverified:
        out += ["", "## Approved events not re-verified tonight", ""] + \
               [f"- {a.get('company')} {a.get('type')} {a.get('from')}" for a in unverified]
    (HERE / "scrape_report.md").write_text("\n".join(out) + "\n")

if __name__ == "__main__":
    asyncio.run(main())
