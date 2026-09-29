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
import asyncio, csv, hashlib, json, re, sys
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
RE_DMY = re.compile(rf"\b(\d{{1,2}})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?{MON}\.?,?(?:\s+(\d{{4}}))?\b", re.I)
RE_MDY = re.compile(rf"\b{MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b,?(?:\s+(\d{{4}}))?", re.I)
RE_NUM = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
RE_WIN = re.compile(rf"\b(early|beginning of|mid|middle of|late|end of|towards the end of)?[\s-]*{MON}\.?\s+(\d{{4}})\b", re.I)
RE_YEAR_LINE = re.compile(r"^(20\d\d)$")
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
RE_RES = re.compile(r"results|interim report|half[- ]?year(?:ly)? report|year[- ]end report|\binterims\b|\bprelim", re.I)
RE_TU = re.compile(r"trading (?:update|statement|report)|pre[- ]close|post[- ]close|business update|"
                   r"interim management statement|\bq[1-4]\b|\bquarter", re.I)
RE_PROV = re.compile(r"provisional|\btbc\b|to be confirmed|indicative|subject to change|expected|\(p\)", re.I)
RE_UPCOMING = re.compile(r"\b(upcoming|forthcoming|future events|key dates|next events?)\b", re.I)
RE_PAST = re.compile(r"\b(past events|previous events|previous dates|past dates|historical|recent past events|archive)\b", re.I)

def classify(text):
    """Return our event type, or None if not a results / TU / AGM line."""
    t = text.lower()
    if RE_AGM.search(t):
        return "AGM"
    strong_tu = re.search(r"trading (?:update|statement|report)|pre[- ]close|post[- ]close|business update", t)
    if RE_RES.search(t) and not strong_tu:
        if re.search(r"half|interim|\bh1\b|six months", t):
            return "H1 results"
        if re.search(r"full[- ]year|final|prelim|annual results|year[- ]end|\bfy", t):
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
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    for el in soup.find_all(attrs={"class": re.compile(r"cookie|consent|gdpr", re.I)}):
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
        if re.fullmatch(r"\d{1,2}", a) and re.fullmatch(MON + r"\.?(\s+\d{4})?", b, re.I):
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
    for rx, order in ((RE_NUM, "num"), (RE_DMY, "dmy"), (RE_MDY, "mdy")):
        for m in rx.finditer(line):
            if not free(*m.span()):
                continue
            if order == "num":
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif order == "dmy":
                d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3)
            else:
                mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
            yearless = y is None
            yield ("exact", d, mo, None if yearless else int(y), m.span())
            taken.append(m.span())
    for m in RE_WIN.finditer(line):
        if not free(*m.span()):
            continue
        q = (m.group(1) or "").lower()
        yield ("window", q, MONTHS[m.group(2).lower()], int(m.group(3)), m.span())
        taken.append(m.span())

def nearby_year(lines, i):
    for k in range(1, 7):
        for j in (i - k, i + k):
            if 0 <= j < len(lines) and RE_YEAR_LINE.match(lines[j]):
                return int(lines[j])
    return None

def residue(line):
    for *_, (a, b) in find_dates(line):
        line = line[:a] + " " * (b - a) + line[b:]
    return re.sub(r"\b20\d\d\b", " ", line)

def has_words(text):
    return len(re.findall(r"[A-Za-z]", residue(text))) >= 3

def neighbour_label(lines, i):
    """Label normally follows the date; skip pure date/year lines. Fall back
    to the preceding line only when the next labelled line is another date."""
    for j in range(i + 1, min(i + 5, len(lines))):
        if not has_words(lines[j]):
            continue
        if list(find_dates(lines[j])):
            break                  # next dated row reached: label must be above
        return lines[j], classify(lines[j])
    for j in range(i - 1, max(i - 3, -1), -1):
        if not has_words(lines[j]):
            continue
        if list(find_dates(lines[j])):
            break
        return lines[j], classify(lines[j])
    return "", None

def extract(lines):
    events, zone = [], None
    for i, line in enumerate(lines):
        if len(line) < 60 and RE_PAST.search(line):
            zone = "past"
        elif len(line) < 60 and RE_UPCOMING.search(line):
            zone = "upcoming"
        for kind, a, mo, y, span in find_dates(line):
            if RE_PERIOD_REF.search(line[max(0, span[0] - 30):span[0]]):
                continue                 # "nine months to 30 September" = period, not event date
            conf = "confirmed"
            if y is None:  # yearless: needs an upcoming heading or a nearby year line
                y = nearby_year(lines, i)
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
            text = (line[:span[0]] + " " + line[span[1]:]).strip(" |-–:")
            if has_words(text):          # label on the same line: judge that alone
                etype = classify(text)
            else:                        # label on a neighbouring line
                text, etype = neighbour_label(lines, i)
            if not etype:
                continue
            if conf == "confirmed" and RE_PROV.search(line + " " + text):
                conf = "provisional"
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
    sources = [r for r in csv.DictReader(open(HERE / "sources.csv")) if r.get("active", "1") != "0"]
    approved = load_json("approved_events.json", [])
    rejected = set(load_json("rejected.json", []))

    sem = asyncio.Semaphore(HTTP_CONCURRENCY)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        results = await asyncio.gather(*(process(s, client, sem) for s in sources))

    # browser fallback for JS / blocked / empty pages
    retry = [i for i, r in enumerate(results)
             if needs_browser(r["status"], r["html"], r["lines"], r["n_dates"])]
    if retry:
        try:
            from playwright.async_api import async_playwright
            bsem = asyncio.Semaphore(BROWSER_CONCURRENCY)
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                async def run(i):
                    async with bsem:
                        src = sources[i]
                        u = src["alt_url"] or src["url"]
                        status, html = await fetch_browser(browser, u)
                        lines = page_lines(html) if status and status < 400 else []
                        n = count_dates(lines)
                        if n > results[i]["n_dates"] or (results[i]["status"] or 0) >= 400 or results[i]["status"] is None:
                            results[i] = {"fetched": u, "method": "browser", "status": status,
                                          "html": html, "lines": lines, "n_dates": n}
                await asyncio.gather(*(run(i) for i in retry))
                await browser.close()
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
        elif len(" ".join(r["lines"])) < 1500:
            outcome = "UNREADABLE"
        elif events:
            outcome = "CLEAN"
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
