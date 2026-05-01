#!/usr/bin/env python3
"""Snapshot GitHub repository traffic + metadata for all owned repos.

Source of truth is data/raw/YYYY-MM-DD/owner__repo.json (committed).
SQLite is rebuilt from those files each run, then today's snapshot is
appended. The Traffic API only retains 14 days, so this script must run
at least every 14 days; daily is recommended.

If npm_packages.json maps a repo to an npm package name, that package's
registry metadata + daily download history are also captured.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "db.sqlite"
SUMMARY_PATH = ROOT / "docs" / "data" / "summary.json"
NPM_PACKAGES_FILE = ROOT / "npm_packages.json"

GITHUB_API = "https://api.github.com"
TOKEN = os.environ.get("GH_TRAFFIC_TOKEN") or os.environ.get("GITHUB_TOKEN")

if not TOKEN:
    sys.exit("error: GH_TRAFFIC_TOKEN (or GITHUB_TOKEN) is required")

INCLUDE_FORKS = os.environ.get("INCLUDE_FORKS", "false").lower() == "true"
INCLUDE_ARCHIVED = os.environ.get("INCLUDE_ARCHIVED", "false").lower() == "true"
# Off by default so a public storage repo never leaks private repo names/traffic.
# Flip to "true" only if the storage repo is private (requires GitHub Pro for Pages).
INCLUDE_PRIVATE = os.environ.get("INCLUDE_PRIVATE", "false").lower() == "true"

session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "omar-github-tracker",
})

NPM_HEADERS = {"User-Agent": "omar-github-tracker"}


def get(url, **params):
    """GET with rate-limit handling. Returns response or None on 404."""
    while True:
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
            sleep = max(reset - int(time.time()), 1) + 2
            print(f"    rate limited, sleeping {sleep}s")
            time.sleep(sleep)
            continue
        if r.status_code in (404, 451):
            return None
        r.raise_for_status()
        return r


def get_paginated(url, **params):
    """Yield items across paginated GitHub responses."""
    params.setdefault("per_page", 100)
    while url:
        r = get(url, **params)
        if r is None:
            return
        for item in r.json():
            yield item
        url = r.links.get("next", {}).get("url")
        params = {}


def discover_repos():
    repos = []
    for repo in get_paginated(f"{GITHUB_API}/user/repos", affiliation="owner"):
        if repo.get("fork") and not INCLUDE_FORKS:
            continue
        if repo.get("archived") and not INCLUDE_ARCHIVED:
            continue
        if repo.get("private") and not INCLUDE_PRIVATE:
            continue
        repos.append(repo)
    return repos


def load_npm_packages():
    """Map of {repo_full_name: npm_package_name}, or empty if no config."""
    if not NPM_PACKAGES_FILE.exists():
        return {}
    try:
        return json.loads(NPM_PACKAGES_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"!! malformed npm_packages.json: {e}")
        return {}


def fetch_npm(package):
    """Registry metadata + full daily-downloads history for an npm package."""
    enc = quote(package, safe="@/")
    r = requests.get(
        f"https://registry.npmjs.org/{enc}",
        headers=NPM_HEADERS, timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    reg = r.json()

    license_val = reg.get("license")
    if isinstance(license_val, dict):
        license_val = license_val.get("type")

    out = {
        "name": package,
        "created_at": (reg.get("time") or {}).get("created"),
        "modified_at": (reg.get("time") or {}).get("modified"),
        "latest_version": (reg.get("dist-tags") or {}).get("latest"),
        "versions": list((reg.get("versions") or {}).keys()),
        "license": license_val,
        "description": reg.get("description"),
        "homepage": reg.get("homepage"),
    }

    # Daily downloads from package creation -> today, in 540-day chunks
    # (npm's range API caps a single request at ~18 months).
    if out["created_at"]:
        start = datetime.fromisoformat(
            out["created_at"].replace("Z", "+00:00")
        ).date()
    else:
        start = (datetime.now(timezone.utc) - timedelta(days=540)).date()
    today = datetime.now(timezone.utc).date()

    daily = {}
    cursor = start
    while cursor <= today:
        end = min(cursor + timedelta(days=540), today)
        url = (
            f"https://api.npmjs.org/downloads/range/"
            f"{cursor.isoformat()}:{end.isoformat()}/{enc}"
        )
        try:
            rr = requests.get(url, headers=NPM_HEADERS, timeout=30)
            if rr.status_code == 404:
                break
            rr.raise_for_status()
            for entry in (rr.json().get("downloads") or []):
                daily[entry["day"]] = entry.get("downloads") or 0
        except Exception as e:
            print(f"    !! npm range {cursor}:{end}: {e}")
        cursor = end + timedelta(days=1)

    out["daily_downloads"] = daily
    return out


def fetch_repo_data(repo, npm_map=None):
    npm_map = npm_map or {}
    full = repo["full_name"]
    out = {"full_name": full, "fetched_at": datetime.now(timezone.utc).isoformat()}

    for key, path in [
        ("clones", "/traffic/clones"),
        ("views", "/traffic/views"),
        ("referrers", "/traffic/popular/referrers"),
        ("paths", "/traffic/popular/paths"),
    ]:
        try:
            r = get(f"{GITHUB_API}/repos/{full}{path}")
            out[key] = r.json() if r is not None else None
        except requests.HTTPError as e:
            print(f"    !! {path}: {e}")
            out[key] = None

    r = get(f"{GITHUB_API}/repos/{full}")
    out["meta"] = r.json() if r is not None else repo

    try:
        out["releases"] = list(get_paginated(f"{GITHUB_API}/repos/{full}/releases"))
    except requests.HTTPError as e:
        print(f"    !! releases: {e}")
        out["releases"] = []

    r = get(f"{GITHUB_API}/repos/{full}/languages")
    out["languages"] = r.json() if r is not None else {}

    package = npm_map.get(full)
    if package:
        print(f"    npm: {package}")
        try:
            out["npm"] = fetch_npm(package)
        except Exception as e:
            print(f"    !! npm fetch: {e}")
            out["npm"] = None

    return out


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS traffic_clones (
            repo TEXT NOT NULL, date TEXT NOT NULL,
            count INTEGER, uniques INTEGER,
            PRIMARY KEY (repo, date)
        );
        CREATE TABLE IF NOT EXISTS traffic_views (
            repo TEXT NOT NULL, date TEXT NOT NULL,
            count INTEGER, uniques INTEGER,
            PRIMARY KEY (repo, date)
        );
        CREATE TABLE IF NOT EXISTS referrers_snap (
            repo TEXT NOT NULL, snap_date TEXT NOT NULL, referrer TEXT NOT NULL,
            count INTEGER, uniques INTEGER,
            PRIMARY KEY (repo, snap_date, referrer)
        );
        CREATE TABLE IF NOT EXISTS paths_snap (
            repo TEXT NOT NULL, snap_date TEXT NOT NULL, path TEXT NOT NULL,
            title TEXT, count INTEGER, uniques INTEGER,
            PRIMARY KEY (repo, snap_date, path)
        );
        CREATE TABLE IF NOT EXISTS repo_snap (
            repo TEXT NOT NULL, snap_date TEXT NOT NULL,
            stars INTEGER, forks INTEGER, watchers INTEGER, open_issues INTEGER,
            subscribers INTEGER, size_kb INTEGER, default_branch TEXT,
            archived INTEGER, fork INTEGER, language TEXT,
            created_at TEXT, updated_at TEXT, pushed_at TEXT,
            PRIMARY KEY (repo, snap_date)
        );
        CREATE TABLE IF NOT EXISTS release_dl (
            repo TEXT NOT NULL, snap_date TEXT NOT NULL, tag TEXT,
            asset_id INTEGER NOT NULL, asset_name TEXT, download_count INTEGER,
            PRIMARY KEY (repo, snap_date, asset_id)
        );
        CREATE TABLE IF NOT EXISTS languages_snap (
            repo TEXT NOT NULL, snap_date TEXT NOT NULL, language TEXT NOT NULL,
            bytes INTEGER,
            PRIMARY KEY (repo, snap_date, language)
        );
        CREATE TABLE IF NOT EXISTS npm_downloads (
            package TEXT NOT NULL, date TEXT NOT NULL,
            downloads INTEGER,
            PRIMARY KEY (package, date)
        );
        CREATE TABLE IF NOT EXISTS npm_meta_snap (
            package TEXT NOT NULL, snap_date TEXT NOT NULL,
            latest_version TEXT, version_count INTEGER,
            created_at TEXT, modified_at TEXT,
            license TEXT, description TEXT,
            PRIMARY KEY (package, snap_date)
        );
    """)


def store(conn, snap_date, data):
    full = data["full_name"]
    meta = data.get("meta") or {}

    for table, key in [("traffic_clones", "clones"), ("traffic_views", "views")]:
        payload = data.get(key) or {}
        for item in (payload.get(key) or []):
            d = item["timestamp"][:10]
            conn.execute(
                f"INSERT OR REPLACE INTO {table} (repo, date, count, uniques) VALUES (?, ?, ?, ?)",
                (full, d, item.get("count"), item.get("uniques")),
            )

    for ref in (data.get("referrers") or []):
        conn.execute(
            "INSERT OR REPLACE INTO referrers_snap VALUES (?, ?, ?, ?, ?)",
            (full, snap_date, ref.get("referrer"), ref.get("count"), ref.get("uniques")),
        )
    for p in (data.get("paths") or []):
        conn.execute(
            "INSERT OR REPLACE INTO paths_snap VALUES (?, ?, ?, ?, ?, ?)",
            (full, snap_date, p.get("path"), p.get("title"), p.get("count"), p.get("uniques")),
        )

    conn.execute(
        """INSERT OR REPLACE INTO repo_snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            full, snap_date,
            meta.get("stargazers_count"), meta.get("forks_count"),
            meta.get("watchers_count"), meta.get("open_issues_count"),
            meta.get("subscribers_count"), meta.get("size"),
            meta.get("default_branch"), int(bool(meta.get("archived"))),
            int(bool(meta.get("fork"))), meta.get("language"),
            meta.get("created_at"), meta.get("updated_at"), meta.get("pushed_at"),
        ),
    )

    for rel in (data.get("releases") or []):
        for asset in (rel.get("assets") or []):
            conn.execute(
                "INSERT OR REPLACE INTO release_dl VALUES (?, ?, ?, ?, ?, ?)",
                (full, snap_date, rel.get("tag_name"), asset.get("id"),
                 asset.get("name"), asset.get("download_count")),
            )

    for lang, byts in (data.get("languages") or {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO languages_snap VALUES (?, ?, ?, ?)",
            (full, snap_date, lang, byts),
        )

    npm = data.get("npm")
    if npm:
        pkg = npm["name"]
        for d, dl in (npm.get("daily_downloads") or {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO npm_downloads VALUES (?, ?, ?)",
                (pkg, d, dl),
            )
        conn.execute(
            "INSERT OR REPLACE INTO npm_meta_snap VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pkg, snap_date, npm.get("latest_version"),
                len(npm.get("versions") or []), npm.get("created_at"),
                npm.get("modified_at"), npm.get("license"), npm.get("description"),
            ),
        )


def write_raw(snap_date, data):
    day_dir = RAW / snap_date
    day_dir.mkdir(parents=True, exist_ok=True)
    safe = data["full_name"].replace("/", "__")
    (day_dir / f"{safe}.json").write_text(json.dumps(data, indent=2, sort_keys=True))


def rebuild_from_raw(conn):
    if not RAW.exists():
        return 0
    n = 0
    for day_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
        snap_date = day_dir.name
        for f in day_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            store(conn, snap_date, data)
            n += 1
    return n


def build_summary(conn, npm_map=None):
    npm_map = npm_map or {}
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)

    repos = [r[0] for r in conn.execute(
        "SELECT DISTINCT repo FROM repo_snap ORDER BY repo"
    ).fetchall()]

    latest_meta_rows = conn.execute("""
        SELECT r.repo, r.stars, r.forks, r.watchers, r.open_issues,
               r.language, r.archived, r.fork, r.pushed_at, r.snap_date,
               r.subscribers
        FROM repo_snap r
        JOIN (SELECT repo, MAX(snap_date) AS d FROM repo_snap GROUP BY repo) m
          ON m.repo = r.repo AND m.d = r.snap_date
    """).fetchall()
    latest_meta = {row[0]: {
        "stars": row[1], "forks": row[2], "watchers": row[3],
        "open_issues": row[4], "language": row[5],
        "archived": bool(row[6]), "fork": bool(row[7]),
        "pushed_at": row[8], "snap_date": row[9],
        "subscribers": row[10],
    } for row in latest_meta_rows}

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repos": {},
        "totals": {},
    }

    for repo in repos:
        clones = conn.execute(
            "SELECT date, count, uniques FROM traffic_clones WHERE repo=? ORDER BY date",
            (repo,)).fetchall()
        views = conn.execute(
            "SELECT date, count, uniques FROM traffic_views WHERE repo=? ORDER BY date",
            (repo,)).fetchall()
        history = conn.execute(
            "SELECT snap_date, stars, forks, watchers, open_issues "
            "FROM repo_snap WHERE repo=? ORDER BY snap_date",
            (repo,)).fetchall()
        latest_referrers = conn.execute("""
            SELECT referrer, count, uniques FROM referrers_snap
            WHERE repo=? AND snap_date=(SELECT MAX(snap_date) FROM referrers_snap WHERE repo=?)
            ORDER BY count DESC
        """, (repo, repo)).fetchall()
        latest_paths = conn.execute("""
            SELECT path, title, count, uniques FROM paths_snap
            WHERE repo=? AND snap_date=(SELECT MAX(snap_date) FROM paths_snap WHERE repo=?)
            ORDER BY count DESC
        """, (repo, repo)).fetchall()
        latest_releases = conn.execute("""
            SELECT tag, asset_name, download_count FROM release_dl
            WHERE repo=? AND snap_date=(SELECT MAX(snap_date) FROM release_dl WHERE repo=?)
            ORDER BY download_count DESC
        """, (repo, repo)).fetchall()
        languages = conn.execute("""
            SELECT language, bytes FROM languages_snap
            WHERE repo=? AND snap_date=(SELECT MAX(snap_date) FROM languages_snap WHERE repo=?)
            ORDER BY bytes DESC
        """, (repo, repo)).fetchall()

        clones_total = sum((c or 0) for _, c, _ in clones)
        views_total = sum((c or 0) for _, c, _ in views)

        repo_entry = {
            "meta": latest_meta.get(repo, {}),
            "clones": [{"date": d, "count": c, "uniques": u} for d, c, u in clones],
            "views": [{"date": d, "count": c, "uniques": u} for d, c, u in views],
            "history": [
                {"date": d, "stars": s, "forks": f, "watchers": w, "open_issues": o}
                for d, s, f, w, o in history
            ],
            "referrers": [
                {"referrer": r, "count": c, "uniques": u} for r, c, u in latest_referrers
            ],
            "paths": [
                {"path": p, "title": t, "count": c, "uniques": u}
                for p, t, c, u in latest_paths
            ],
            "releases": [
                {"tag": t, "asset": n, "downloads": d} for t, n, d in latest_releases
            ],
            "languages": [{"language": l, "bytes": b} for l, b in languages],
            "all_time": {
                "clones": clones_total,
                "views": views_total,
            },
        }

        package = npm_map.get(repo)
        if package:
            daily_rows = conn.execute(
                "SELECT date, downloads FROM npm_downloads "
                "WHERE package=? ORDER BY date",
                (package,)).fetchall()
            meta_row = conn.execute("""
                SELECT latest_version, version_count, created_at, license, description
                FROM npm_meta_snap WHERE package=?
                ORDER BY snap_date DESC LIMIT 1
            """, (package,)).fetchone()
            all_time_dl = sum((dl or 0) for _, dl in daily_rows)
            d30 = conn.execute(
                "SELECT COALESCE(SUM(downloads),0) FROM npm_downloads "
                "WHERE package=? AND date >= date('now','-30 days')",
                (package,)).fetchone()[0]
            d7 = conn.execute(
                "SELECT COALESCE(SUM(downloads),0) FROM npm_downloads "
                "WHERE package=? AND date >= date('now','-7 days')",
                (package,)).fetchone()[0]
            repo_entry["npm"] = {
                "name": package,
                "latest_version": meta_row[0] if meta_row else None,
                "version_count": meta_row[1] if meta_row else 0,
                "created_at": meta_row[2] if meta_row else None,
                "license": meta_row[3] if meta_row else None,
                "description": meta_row[4] if meta_row else None,
                "all_time": all_time_dl,
                "d30": d30,
                "d7": d7,
                "daily": [{"date": d, "downloads": dl} for d, dl in daily_rows],
            }

        out["repos"][repo] = repo_entry

    def sum_q(sql):
        return conn.execute(sql).fetchone()[0] or 0

    out["totals"] = {
        "repo_count": len(repos),
        "total_stars": sum((m.get("stars") or 0) for m in latest_meta.values()),
        "total_forks": sum((m.get("forks") or 0) for m in latest_meta.values()),
        # subscribers_count is the real "Watching" count; watchers_count is
        # a legacy alias for stargazers and would just duplicate stars.
        "total_watchers": sum((m.get("subscribers") or 0) for m in latest_meta.values()),

        "clones_all_time": sum_q("SELECT COALESCE(SUM(count),0) FROM traffic_clones"),
        "views_all_time": sum_q("SELECT COALESCE(SUM(count),0) FROM traffic_views"),
        "unique_cloners_all_time": sum_q("SELECT COALESCE(SUM(uniques),0) FROM traffic_clones"),
        "unique_visitors_all_time": sum_q("SELECT COALESCE(SUM(uniques),0) FROM traffic_views"),

        "clones_30d": sum_q(
            "SELECT COALESCE(SUM(count),0) FROM traffic_clones "
            "WHERE date >= date('now', '-30 days')"),
        "views_30d": sum_q(
            "SELECT COALESCE(SUM(count),0) FROM traffic_views "
            "WHERE date >= date('now', '-30 days')"),
        "unique_visitors_30d": sum_q(
            "SELECT COALESCE(SUM(uniques),0) FROM traffic_views "
            "WHERE date >= date('now', '-30 days')"),
        "unique_cloners_30d": sum_q(
            "SELECT COALESCE(SUM(uniques),0) FROM traffic_clones "
            "WHERE date >= date('now', '-30 days')"),

        "npm_packages_count": len(npm_map),
        "npm_downloads_all_time": sum_q(
            "SELECT COALESCE(SUM(downloads),0) FROM npm_downloads"),
        "npm_downloads_30d": sum_q(
            "SELECT COALESCE(SUM(downloads),0) FROM npm_downloads "
            "WHERE date >= date('now', '-30 days')"),
        "npm_downloads_7d": sum_q(
            "SELECT COALESCE(SUM(downloads),0) FROM npm_downloads "
            "WHERE date >= date('now', '-7 days')"),
    }

    SUMMARY_PATH.write_text(json.dumps(out, indent=2, sort_keys=True))


def main():
    DATA.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)

    snap_date = datetime.now(timezone.utc).date().isoformat()
    print(f"snapshot date: {snap_date}")

    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    n = rebuild_from_raw(conn)
    print(f"rebuilt SQLite from {n} historical snapshots")

    npm_map = load_npm_packages()
    if npm_map:
        print(f"npm packages tracked: {sorted(npm_map.values())}")

    repos = discover_repos()
    print(f"discovered {len(repos)} repos")

    for i, repo in enumerate(repos, 1):
        full = repo["full_name"]
        print(f"  [{i}/{len(repos)}] {full}")
        try:
            data = fetch_repo_data(repo, npm_map)
            store(conn, snap_date, data)
            write_raw(snap_date, data)
        except requests.HTTPError as e:
            print(f"    !! skipping {full}: {e}")
            continue

    conn.commit()
    build_summary(conn, npm_map)
    conn.close()
    print(f"summary -> {SUMMARY_PATH.relative_to(ROOT)}")

    # Stamp the dashboard's asset URLs so browsers fetch fresh CSS/JS
    # whenever the data updates. Without this, browser caches the old
    # styles.css and the new layout never appears until manual reload.
    bump_asset_version(snap_date)


def bump_asset_version(version):
    html_path = ROOT / "docs" / "index.html"
    if not html_path.exists():
        return
    content = html_path.read_text()
    new = re.sub(
        r'(href="styles\.css)\?v=[^"]*(")',
        rf'\1?v={version}\2',
        content,
    )
    new = re.sub(
        r'(src="app\.js)\?v=[^"]*(")',
        rf'\1?v={version}\2',
        new,
    )
    if new != content:
        html_path.write_text(new)
        print(f"asset version -> {version}")


if __name__ == "__main__":
    main()
