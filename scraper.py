#!/usr/bin/env python3
"""
scraper.py — self-running legislative data collector.

Mirrors the architecture Ron described ("it runs on its own"):
a scheduled job that scrapes public legislative web pages — NO paid API —
classifies each bill with an LLM, scores impact, and writes to a SQLite DB
that the web front end reads from.

Data source: LegiScan's PUBLIC SEARCH PAGES (the human-facing HTML at
legiscan.com/gaits/search), not the API. This is ordinary web scraping of
public pages, exactly like Ron's "scraping websites" description.

Run once:      python scraper.py --once
Run scheduled: python scraper.py --schedule   (nightly at 22:00, like Legets' 10pm agent)

Requires: requests, beautifulsoup4, lxml  (+ optional anthropic for live AI classification)
"""

import argparse, sqlite3, time, re, sys, os, json, datetime, urllib.parse
import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legets.db")
UA = {"User-Agent": "Mozilla/5.0 (compatible; HRLegTracker/1.0; internal-eval)"}

# HR-relevant search topics. Each becomes a scraped query against public pages.
# These mirror the categories Legets surfaces (Payroll, Benefits, etc.).
TOPICS = [
    ("minimum wage", "Payroll"),
    ("paid leave", "Benefits"),
    ("sick leave", "Benefits"),
    ("paid family leave", "Benefits"),
    ("meal periods", "Payroll"),
    ("pay transparency", "Payroll"),
    ("salary disclosure", "Payroll"),
    ("noncompete", "Employee Relations"),
    ("non-compete", "Employee Relations"),
    ("restrictive covenant", "Employee Relations"),
    ("independent contractor", "Employment Taxes"),
    ("worker classification", "Employment Taxes"),
    ("pay equity", "Payroll"),
    ("bereavement leave", "Benefits"),
    ("workplace discrimination", "Employee Relations"),
    ("personnel records", "Employee Relations"),
    ("wage theft", "Payroll"),
    ("overtime", "Payroll"),
]

# States to cover. Trimmed to a strong, high-activity set rather than all 51 —
# broad single/multi-word searches across all 50 states produced excessive
# noise (15,000+ mostly-irrelevant bills in testing). This list is easy to
# expand later once search precision is tuned further.
STATES = ["CA","IL","TX","FL","CO","NY","WA","MA","NJ","MN","PA","OH",
          "NC","GA","VA","MI","OR","CT","MD","US"]

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS bills(
        id TEXT PRIMARY KEY, state TEXT, number TEXT, title TEXT,
        status TEXT, last_action TEXT, last_action_date TEXT,
        category TEXT, impact INTEGER, memo TEXT, url TEXT,
        topic TEXT, scraped_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT, finished_at TEXT, bills_found INTEGER, status TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS rulings(
        id TEXT PRIMARY KEY, court TEXT, case_name TEXT, ruling_date TEXT,
        topic TEXT, summary TEXT, url TEXT, source TEXT, scraped_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS comments(
        id TEXT PRIMARY KEY, agency TEXT, rule TEXT, closes TEXT,
        status TEXT, summary TEXT, url TEXT, source TEXT, scraped_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS news(
        id TEXT PRIMARY KEY, title TEXT, summary TEXT, source TEXT,
        published TEXT, url TEXT, scraped_at TEXT
    )""")
    con.commit()
    return con

# ---------------------------------------------------------------------------
# SCRAPE  (LegiScan's official FREE public API — not scraping, not a paid vendor)
# ---------------------------------------------------------------------------
def legiscan_search(state, query, session, api_key):
    """
    Query LegiScan's free public API (op=getSearch). Free tier: 30,000
    queries/month, no cost. This is the same API tested manually earlier
    in the build-vs-buy evaluation. Docs:
    https://legiscan.com/gaits/documentation/legiscan-api

    Switched to this after confirming LegiScan's HTML search pages return
    HTTP 403 to automated requests (active bot protection) — see build notes.
    """
    out = []
    url = "https://api.legiscan.com/"
    # Multi-word topics are quoted as exact phrases — LegiScan's default matching
    # is OR-across-words, which returns heavy noise on broad terms (confirmed
    # earlier in this evaluation: "employment" alone returned 397 mostly
    # irrelevant bills). Phrase-quoting cuts that dramatically.
    q = f'"{query}"' if " " in query else query
    params = {"key": api_key, "op": "getSearch", "state": state, "query": q}
    try:
        r = session.get(url, params=params, headers=UA, timeout=20)
        if r.status_code != 200:
            print(f"  [{state}/{query}] HTTP {r.status_code}", file=sys.stderr)
            return out
        data = r.json()
        if data.get("status") != "OK":
            alert = (data.get("alert") or {}).get("message", "unknown error")
            print(f"  [{state}/{query}] API error: {alert}", file=sys.stderr)
            return out
        results = data.get("searchresult", {}) or {}
        for key, bill in results.items():
            if key == "summary" or not isinstance(bill, dict):
                continue
            out.append({
                "state": bill.get("state", state),
                "number": bill.get("bill_number", ""),
                "title": bill.get("title", ""),
                "url": bill.get("url", ""),
                "last_action": bill.get("last_action", "") or "",
                "last_action_date": bill.get("last_action_date", "") or "",
            })
        time.sleep(0.3)  # polite pacing, well within the free-tier rate limit
    except Exception as e:
        print(f"  [{state}/{query}] ERROR {e}", file=sys.stderr)
    return out


def normalize_status(action_text):
    a = (action_text or "").lower()
    if re.search(r"(signed|chaptered|enrolled|public act|effective|approved by governor)", a):
        return "signed"
    if re.search(r"(vetoed|died|failed)", a):
        return "vetoed"
    return "active"

# ---------------------------------------------------------------------------
# CLASSIFY + SCORE  (LLM layer — the "AI" Ron confirmed is Claude)
# ---------------------------------------------------------------------------
PROFILE = ("250-employee multi-state services company operating in CA, IL, CO, TX, FL "
           "with federal exposure")

def classify_and_score(bill, default_category):
    """
    Assign HR category + 0-10 impact score + a short memo.

    If ANTHROPIC_API_KEY is set, this calls Claude for real (this is the exact
    layer Ron uses). Otherwise it falls back to a deterministic keyword scorer
    so the pipeline still runs end-to-end without credentials.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        try:
            return _claude_classify(bill, key)
        except Exception as e:
            print(f"  [AI fallback: {e}]", file=sys.stderr)
    return _heuristic_classify(bill, default_category)


def _claude_classify(bill, key):
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    prompt = f"""You are an HR compliance analyst. For this bill, respond with ONLY a JSON object, no prose.

Bill: {bill['state']} {bill['number']} — "{bill['title']}"
Latest action: {bill.get('last_action','(unknown)')}

Customer profile to score impact against: {PROFILE}

Return JSON with keys:
- category: one of ["Payroll","Benefits","Employee Relations","Employment Taxes","Recruiting","Termination","Disability","Other"]
- impact: integer 0-10 (how much this bill affects the customer profile)
- memo: 1-2 sentence plain-English operational impact for their HR team

JSON only."""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    text = re.sub(r"^```json|```$", "", text.strip()).strip()
    data = json.loads(text)
    return data["category"], int(data["impact"]), data["memo"]


def _heuristic_classify(bill, default_category):
    """Deterministic fallback: keyword-driven category + impact."""
    t = bill["title"].lower()
    cat = default_category
    if any(w in t for w in ["contractor", "classification", "gig", "worker empowerment",
                            "app-based", "portable benefit"]):
        cat = "Employment Taxes"
    elif any(w in t for w in ["leave", "bereavement", "family medical", "family and medical", "sick"]):
        cat = "Benefits"
    elif any(w in t for w in ["wage", "pay", "salary", "overtime", "meal period", "compensation", "equity"]):
        cat = "Payroll"
    elif any(w in t for w in ["noncompete", "non-compete", "covenant", "discrimination",
                              "records", "rights", "empowerment", "labor relations"]):
        cat = "Employee Relations"
    # impact heuristic: relevance to broad multi-state employer
    score = 4
    if any(w in t for w in ["minimum wage", "paid family", "family and medical", "classification",
                            "pay transparency", "pay equity", "meal period", "worker empowerment"]):
        score = 8
    elif any(w in t for w in ["leave", "wage", "overtime", "discrimination", "noncompete",
                              "non-compete", "contractor", "covenant", "records"]):
        score = 6
    # niche/narrow bills score low
    if any(w in t for w in ["agricultural", "firefighter", "airline", "school", "public employee",
                            "cabin crew", "petroleum", "military"]):
        score = min(score, 3)
    memo = f"Auto-classified as {cat}. Heuristic impact {score}/10 for the target profile. (Set ANTHROPIC_API_KEY for Claude-generated memos, as the production Legets does.)"
    return cat, score, memo

# ---------------------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------------------
def fetch_hr_news(session, limit=12):
    """
    Pull recent HR / employment-law headlines from FREE public RSS feeds.
    Uses stdlib xml parsing — no extra pip dependency. Degrades to [] on error.
    """
    import xml.etree.ElementTree as ET
    feeds = [
        ("HR Dive", "https://www.hrdive.com/feeds/news/"),
        ("JD Supra – Labor & Employment", "https://www.jdsupra.com/legalnews/rss/category/labor-employment-law/"),
        ("SHRM", "https://www.shrm.org/rss/pages/rss.aspx"),
    ]
    out, seen = [], set()
    for source, url in feeds:
        try:
            r = session.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                print(f"  [news/{source}] HTTP {r.status_code}", file=sys.stderr)
                continue
            root = ET.fromstring(r.content)
            # RSS <item> under channel; handle common namespaces loosely
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc = (item.findtext("description") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if not title or link in seen:
                    continue
                seen.add(link)
                # strip any HTML tags from description, trim length
                desc = re.sub(r"<[^>]+>", "", desc)
                out.append({
                    "id": link or title,
                    "title": title,
                    "summary": desc[:220],
                    "source": source,
                    "published": pub,
                    "url": link,
                })
            time.sleep(0.5)
        except Exception as e:
            print(f"  [news/{source}] ERROR {e}", file=sys.stderr)
    # newest first isn't reliable across feeds without date parsing; keep feed order, cap total
    return out[:limit]


def run_once():
    con = init_db()
    api_key = os.environ.get("LEGISCAN_API_KEY")
    if not api_key:
        print("ERROR: LEGISCAN_API_KEY is not set. Add it as a GitHub Actions secret "
              "(Settings -> Secrets and variables -> Actions -> New repository secret).",
              file=sys.stderr)
        sys.exit(1)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"[{started}] scrape run starting — {len(STATES)} states x {len(TOPICS)} topics "
          f"({len(STATES)*len(TOPICS)} API queries, free tier allows 30,000/month)")
    session = requests.Session()
    seen = {}
    for state in STATES:
        for query, default_cat in TOPICS:
            results = legiscan_search(state, query, session, api_key)
            for b in results:
                bid = f"{b['state']}-{b['number']}"
                if bid in seen:
                    continue  # dedupe across topics
                try:
                    b["status"] = normalize_status(b["last_action"])
                    cat, impact, memo = classify_and_score(b, default_cat)
                    b.update({"category": cat, "impact": impact, "memo": memo,
                              "topic": query, "id": bid,
                              "scraped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
                    seen[bid] = b
                except Exception as e:
                    print(f"  [classify error on {bid}: {e}] — skipping this bill", file=sys.stderr)
                    continue
            print(f"  {state}/{query}: {len(results)} rows (total unique {len(seen)})")

    # write to DB (replace snapshot)
    con.execute("DELETE FROM bills")
    for b in seen.values():
        con.execute("""INSERT OR REPLACE INTO bills
            (id,state,number,title,status,last_action,last_action_date,category,impact,memo,url,topic,scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (b["id"], b["state"], b["number"], b["title"], b["status"],
             b["last_action"], b["last_action_date"], b["category"], b["impact"],
             b["memo"], b["url"], b["topic"], b["scraped_at"]))
    finished = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    con.execute("INSERT INTO runs(started_at,finished_at,bills_found,status) VALUES(?,?,?,?)",
                (started, finished, len(seen), "ok"))
    # ---- collect from the free non-legislature sources (courts + agencies) ----
    try:
        import sources
        print("scraping court rulings (CourtListener, free)...")
        rulings = sources.fetch_court_rulings()
        con.execute("DELETE FROM rulings")
        now2 = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        for r in rulings:
            con.execute("""INSERT OR REPLACE INTO rulings
                (id,court,case_name,ruling_date,topic,summary,url,source,scraped_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["court"], r["case"], r["date"], r["topic"],
                 r["summary"], r["url"], r["source"], now2))
        print(f"  {len(rulings)} rulings stored")

        print("scraping comment periods (regulations.gov, free)...")
        comments = sources.fetch_comment_periods()
        con.execute("DELETE FROM comments")
        for c in comments:
            con.execute("""INSERT OR REPLACE INTO comments
                (id,agency,rule,closes,status,summary,url,source,scraped_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (c["id"], c["agency"], c["rule"], c["closes"], c["status"],
                 c["summary"], c["url"], c["source"], now2))
        print(f"  {len(comments)} comment periods stored")
    except Exception as e:
        print(f"  [sources] skipped: {e}")

    # ---- HR news headlines (free RSS feeds) ----
    try:
        print("fetching HR news (free RSS feeds)...")
        news = fetch_hr_news(session)
        con.execute("DELETE FROM news")
        now3 = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        for n in news:
            con.execute("""INSERT OR REPLACE INTO news
                (id,title,summary,source,published,url,scraped_at)
                VALUES (?,?,?,?,?,?,?)""",
                (n["id"], n["title"], n["summary"], n["source"],
                 n["published"], n["url"], now3))
        print(f"  {len(news)} news items stored")
    except Exception as e:
        print(f"  [news] skipped: {e}")

    print("checkpoint: committing to DB...")
    con.commit()
    print("checkpoint: exporting JSON...")
    # also export a JSON snapshot the static UI can read directly
    export_json(con)
    print("checkpoint: closing DB connection...")
    con.close()
    print(f"[{finished}] done — {len(seen)} unique bills written to DB + JSON snapshot")


def export_json(con):
    rows = con.execute("SELECT id,state,number,title,status,last_action,last_action_date,category,impact,memo,url,scraped_at FROM bills").fetchall()
    cols = ["id","st","num","title","status","action","date","cat","impact","memo","url","scraped_at"]
    bills = [dict(zip(cols, r)) for r in rows]
    # rulings
    rcols = ["id","court","case","date","topic","summary","url","source"]
    rulings = [dict(zip(rcols, r)) for r in con.execute(
        "SELECT id,court,case_name,ruling_date,topic,summary,url,source FROM rulings ORDER BY ruling_date DESC").fetchall()]
    # comments
    ccols = ["id","agency","rule","closes","status","summary","url","source"]
    comments = [dict(zip(ccols, r)) for r in con.execute(
        "SELECT id,agency,rule,closes,status,summary,url,source FROM comments ORDER BY closes ASC").fetchall()]
    # news
    ncols = ["id","title","summary","source","published","url"]
    news = [dict(zip(ncols, r)) for r in con.execute(
        "SELECT id,title,summary,source,published,url FROM news").fetchall()]
    run = con.execute("SELECT started_at,finished_at,bills_found FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    snapshot = {"generated_at": run[1] if run else None,
                "bill_count": len(bills), "bills": bills,
                "rulings": rulings, "comments": comments, "news": news}
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    with open(out, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"  exported {len(bills)} bills, {len(rulings)} rulings, {len(comments)} comments, {len(news)} news -> {out}")


def run_schedule():
    """Self-running mode: scrape now, then every night at 22:00 (Legets runs at 10pm)."""
    print("scheduler started — will scrape nightly at 22:00. Ctrl-C to stop.")
    run_once()
    while True:
        now = datetime.datetime.now()
        nxt = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        wait = (nxt - now).total_seconds()
        print(f"next run at {nxt} ({int(wait)}s from now)")
        time.sleep(wait)
        run_once()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single scrape now")
    ap.add_argument("--schedule", action="store_true", help="run continuously, nightly at 22:00")
    args = ap.parse_args()
    print(f"Python {sys.version.split()[0]} | cwd={os.getcwd()} | DB_PATH={os.path.abspath(DB_PATH)}")
    try:
        if args.schedule:
            run_schedule()
        else:
            run_once()
    except BaseException:
        import traceback
        print("=== SCRAPER FAILED — full traceback below ===", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
