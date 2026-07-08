#!/usr/bin/env python3
"""
app.py — the backend API, decoupled from the front end (exactly as Ron described:
"the back end is agnostic to the front end").

Serves the scraped + classified bill data from SQLite as JSON, and serves the
static web UI. The React/HTML front end calls /api/bills instead of holding data.

Run: python app.py    ->  http://localhost:8000
"""
import sqlite3, os, json
from flask import Flask, jsonify, send_from_directory, request

BASE = os.path.dirname(__file__)
DB = os.path.join(BASE, "..", "data", "legets.db")
WEB = os.path.join(BASE, "..", "web")

app = Flask(__name__, static_folder=WEB, static_url_path="")

def q(sql, args=()):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close(); return rows

@app.route("/api/bills")
def bills():
    state = request.args.get("state")
    cat = request.args.get("category")
    sql = "SELECT id,state st,number num,title,status,last_action action,last_action_date date,category cat,impact,memo,url,scraped_at FROM bills WHERE 1=1"
    args = []
    if state: sql += " AND state=?"; args.append(state)
    if cat: sql += " AND category=?"; args.append(cat)
    sql += " ORDER BY impact DESC"
    return jsonify(q(sql, args))

@app.route("/api/stats")
def stats():
    total = q("SELECT COUNT(*) c FROM bills")[0]["c"]
    high = q("SELECT COUNT(*) c FROM bills WHERE impact>=7")[0]["c"]
    signed = q("SELECT COUNT(*) c FROM bills WHERE status='signed'")[0]["c"]
    states = q("SELECT COUNT(DISTINCT state) c FROM bills")[0]["c"]
    run = q("SELECT finished_at, bills_found FROM runs ORDER BY id DESC LIMIT 1")
    by_cat = q("SELECT category, COUNT(*) c FROM bills GROUP BY category ORDER BY c DESC")
    by_state = q("SELECT state, COUNT(*) c, AVG(impact) avg FROM bills GROUP BY state ORDER BY c DESC")
    rulings = q("SELECT COUNT(*) c FROM rulings")[0]["c"]
    comments = q("SELECT COUNT(*) c FROM comments")[0]["c"]
    recent_runs = q("SELECT started_at,finished_at,bills_found,status FROM runs ORDER BY id DESC LIMIT 10")
    return jsonify({"total": total, "high": high, "signed": signed, "states": states,
                    "last_run": run[0] if run else None, "by_cat": by_cat, "by_state": by_state,
                    "rulings": rulings, "comments": comments, "recent_runs": recent_runs})

@app.route("/api/rulings")
def rulings():
    return jsonify(q("SELECT id,court,case_name 'case',ruling_date date,topic,summary,url,source FROM rulings ORDER BY ruling_date DESC"))

@app.route("/api/comments")
def comments():
    return jsonify(q("SELECT id,agency,rule,closes,status,summary,url,source FROM comments ORDER BY closes ASC"))

@app.route("/api/news")
def news():
    return jsonify(q("SELECT id,title,summary,source,published,url FROM news"))

@app.route("/")
def index():
    return send_from_directory(WEB, "index.html")

if __name__ == "__main__":
    print("Legets clone API on http://localhost:8000  (UI + /api/bills + /api/stats)")
    app.run(port=8000, debug=False)
