# == Walking Skeleton ==
# General proof of concept-- the card moves and the event logs from a click
# Sprint 2: WIP limits with visible counters, staleness indicators, and
#           automatic warnings + logging for limit violations and corrections.
# Sprint 2b: minimal add-card endpoint so the board isn't a dead end.
# Sprint 3: user login + roster, card blocking + comments, personalized
#           view (overdue + sprint progress), and a weekly DAU export.
# Sprint 4: coordinator view (stalled cards, overcommitted members, team
#           stats), plus edge-case fixes and UI polish.
# Sprint 5: the three measures computed as gates -- weekly DAU against 75%,
#           self-correction within 5 minutes against 80%, lag reduction
#           against the recorded Sprint 1 baseline against 50% -- plus a
#           critical-issue log and a Done column so "done" is real.

import sqlite3
import os
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from io import StringIO
from flask import (
    Flask, render_template, request, jsonify, session, redirect, url_for
)
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))
socketio = SocketIO(app, cors_allowed_origins="*")

DB = "skeleton.db"

# Sprint 2 -- WIP limit per column. None means no limit.
WIP_LIMITS = {
    "todo": None,
    "in_progress": 3,
    "done": None,
}

# Sprint 2 -- staleness threshold for idle cards
STALENESS_THRESHOLD = timedelta(days=3)
STALE_DAYS = STALENESS_THRESHOLD.days

# Sprint 3 -- sprint length used by the personalized view's progress bar.
SPRINT_LENGTH_DAYS = 14

# Sprint 4 -- how many active cards a single member should hold before a
# coordinator treats them as overcommitted.
MEMBER_WIP_LIMIT = 3

# Sprint 5 -- the three measure thresholds, decided back in week 2.
# They live here so the export, the metrics page, and the coordinator
# view all compare against the same numbers.
MEASURE_THRESHOLDS = {
    "dau_pct": 75.0,          # Measure 1: 3+ days/week, 75% of active users
    "self_correction_pct": 80.0,  # Measure 2: corrected within 5 minutes
    "lag_reduction_pct": 50.0,    # Measure 3: reduction from Sprint 1 baseline
}
SELF_CORRECTION_WINDOW_SECONDS = 300  # "within five minutes"
BASELINE_KEY = "sprint1_avg_lag_hours"


def get_db():
    # Open a connection to the SQLite file
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # Create tables if they don't exist, run migrations, and seed demo data
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            team_id INTEGER,
            role TEXT NOT NULL DEFAULT 'member'
        );
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            col TEXT NOT NULL DEFAULT 'todo',
            created_at TEXT,
            owner_id INTEGER,
            due_date TEXT,
            blocked INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            team_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            card_id INTEGER NOT NULL,
            user TEXT NOT NULL,
            from_col TEXT NOT NULL,
            to_col TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'move',
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY,
            card_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS logins (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        );
        -- Sprint 5 -- recorded baselines, so Measure 3 compares against a
        -- number that was written down rather than one we invented later.
        CREATE TABLE IF NOT EXISTS baselines (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL,
            recorded_at TEXT NOT NULL
        );
        -- Sprint 5 -- the critical-issue log. "No unresolved critical bugs"
        -- is an exit criterion, so it needs to be countable.
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'critical',
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
    """)

    # Sprint 2 migrations -- add columns to any pre-Sprint-2 database.
    for stmt in (
        "ALTER TABLE cards ADD COLUMN created_at TEXT",
        "ALTER TABLE events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'move'",
        "ALTER TABLE events ADD COLUMN resolved_at TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass

    # Sprint 3 migrations -- add owner/due_date/blocked/team_id to cards.
    for stmt in (
        "ALTER TABLE cards ADD COLUMN owner_id INTEGER",
        "ALTER TABLE cards ADD COLUMN due_date TEXT",
        "ALTER TABLE cards ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE cards ADD COLUMN block_reason TEXT",
        "ALTER TABLE cards ADD COLUMN team_id INTEGER",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass

    # Sprint 4 migration -- add the role column to users.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
    except sqlite3.OperationalError:
        pass

    # Backfill created_at on any card that predates the migration
    conn.execute(
        "UPDATE cards SET created_at = ? WHERE created_at IS NULL",
        (datetime.now().astimezone().isoformat(),)
    )

    # Sprint 3 -- seed a demo team and users on first run.
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0:
        conn.execute("INSERT INTO teams (name) VALUES (?)", ("Trace Team",))
    team_id = conn.execute("SELECT id FROM teams LIMIT 1").fetchone()[0]

    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        demo_users = [
            ("alice", "password", "Alice", "member"),
            ("bob",   "password", "Bob",   "member"),
            ("carol", "password", "Carol", "member"),
            ("dana",  "password", "Dana",  "coordinator"),
        ]
        for username, pw, display, role in demo_users:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, team_id, role) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, generate_password_hash(pw), display, team_id, role)
            )

    # Sprint 4 -- make sure an existing database also has a coordinator.
    if conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'coordinator'"
    ).fetchone()[0] == 0:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = 'dana'"
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET role = 'coordinator' WHERE id = ?",
                (existing["id"],)
            )
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, team_id, role) "
                "VALUES (?, ?, ?, ?, ?)",
                ("dana", generate_password_hash("password"), "Dana", team_id, "coordinator")
            )

    # Backfill team_id on any pre-Sprint-3 card
    conn.execute("UPDATE cards SET team_id = ? WHERE team_id IS NULL", (team_id,))

    # Seed one card if the table is empty
    if conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO cards (title, col, created_at, team_id) VALUES (?, ?, ?, ?)",
            ("First card", "todo", datetime.now().astimezone().isoformat(), team_id)
        )
    conn.commit()
    conn.close()


# == Sprint 3 -- auth helpers ==
def login_required(f):
    # Redirect anonymous visitors to /login before they hit a protected route.
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def current_user(conn):
    # Fetch the logged-in user's row, or None if not signed in.
    uid = session.get("user_id")
    if not uid:
        return None
    return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def current_team_id(conn):
    user = current_user(conn)
    return user["team_id"] if user else None


# == Sprint 2 helpers (unchanged behavior) ==
def column_counts(conn):
    # Sprint 2 -- used by the visible WIP counters on each column header.
    rows = conn.execute(
        "SELECT col, COUNT(*) AS n FROM cards GROUP BY col"
    ).fetchall()
    return {r["col"]: r["n"] for r in rows}


def last_activity(conn):
    # Sprint 2 -- per-card last activity for staleness.
    events = {
        r["card_id"]: r["last"] for r in conn.execute(
            "SELECT card_id, MAX(timestamp) AS last FROM events GROUP BY card_id"
        ).fetchall()
    }
    out = {}
    for row in conn.execute("SELECT id, created_at FROM cards").fetchall():
        created = row["created_at"]
        last_ev = events.get(row["id"])
        if created and last_ev:
            out[row["id"]] = max(created, last_ev)
        else:
            out[row["id"]] = created or last_ev
    return out


def idle_days(card, last_ts):
    # Sprint 4 -- staleness math pulled out of is_stale so the coordinator
    # view can report *how* idle a card is, not just whether it's idle.
    if not last_ts:
        return None
    try:
        ts = datetime.fromisoformat(last_ts)
    except ValueError:
        return None
    now = datetime.now().astimezone()
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return (now - ts).total_seconds() / 86400.0


def is_stale(card, last_ts):
    # Sprint 2 -- a card is stale if it isn't Done and its last activity
    # is older than STALENESS_THRESHOLD.
    if card["col"] == "done":
        return False
    days = idle_days(card, last_ts)
    return days is not None and days > STALE_DAYS


def is_overdue(due_date, col):
    # Sprint 4 -- edge-case fix. The old check compared a date string
    # against a full ISO timestamp, which marked a card overdue on its
    # own due day. Compare date parts only, and never overdue when Done.
    if not due_date or col == "done":
        return False
    today = datetime.now().astimezone().date().isoformat()
    return due_date[:10] < today


def log_event(conn, card_id, user, from_col, to_col, event_type="move"):
    # Sprint 2 -- one place to write an event row.
    conn.execute(
        "INSERT INTO events (card_id, user, from_col, to_col, timestamp, event_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (card_id, user, from_col, to_col,
         datetime.now().astimezone().isoformat(), event_type)
    )


def check_wip_and_log(conn, card_id, user, from_col, to_col):
    # Sprint 2 -- detect WIP violations and corrections in one pass.
    now = datetime.now().astimezone().isoformat()

    dest_limit = WIP_LIMITS.get(to_col)
    dest_count = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE col = ?", (to_col,)
    ).fetchone()[0]
    is_violation = dest_limit is not None and dest_count > dest_limit

    event_type = "violation" if is_violation else "move"
    log_event(conn, card_id, user, from_col, to_col, event_type)

    src_limit = WIP_LIMITS.get(from_col)
    if src_limit is not None:
        src_count = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE col = ?", (from_col,)
        ).fetchone()[0]
        if src_count <= src_limit:
            open_violations = conn.execute(
                "SELECT id FROM events "
                "WHERE to_col = ? AND event_type = 'violation' "
                "AND resolved_at IS NULL",
                (from_col,)
            ).fetchall()
            if open_violations:
                for row in open_violations:
                    conn.execute(
                        "UPDATE events SET resolved_at = ? WHERE id = ?",
                        (now, row["id"])
                    )
                log_event(conn, card_id, user, from_col, to_col,
                          event_type="correction")

    return event_type


# == Sprint 5 -- baseline + issue helpers ==
def get_baseline(conn, key):
    # Read a recorded baseline, or None if nothing has been written yet.
    row = conn.execute(
        "SELECT value FROM baselines WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_baseline(conn, key, value):
    # Write (or overwrite) a baseline. The timestamp is the audit trail.
    conn.execute(
        "INSERT INTO baselines (key, value, recorded_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "recorded_at = excluded.recorded_at",
        (key, value, datetime.now().astimezone().isoformat())
    )


# == Sprint 3 -- DAU + export helpers ==
def compute_dau(conn, days=14):
    # Count distinct users who logged in on each day, for the last N days.
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT substr(timestamp, 1, 10) AS day, "
        "       COUNT(DISTINCT user_id) AS users "
        "FROM logins WHERE timestamp >= ? "
        "GROUP BY day ORDER BY day",
        (since,)
    ).fetchall()
    return [{"day": r["day"], "users": r["users"]} for r in rows]


def compute_dau_weekly(conn, weeks=4):
    # Sprint 5 -- Measure 1, done properly. For each 7-day window, count
    # the users who opened the board on 3 or more distinct days, and
    # express them as a share of the users active in that window.
    #
    # This is the number the 75% threshold is actually about. The old
    # daily DAU count could not answer it.
    since = (datetime.now().astimezone() - timedelta(days=weeks * 7)).isoformat()
    rows = conn.execute(
        "SELECT user_id, substr(timestamp, 1, 10) AS day "
        "FROM logins WHERE timestamp >= ?",
        (since,)
    ).fetchall()

    by_day = defaultdict(set)
    for r in rows:
        by_day[r["day"]].add(r["user_id"])

    now = datetime.now().astimezone()
    out = []
    for w in range(weeks):
        end = (now - timedelta(days=w * 7)).date()
        start = end - timedelta(days=7)
        start_day = start.isoformat()
        end_day = end.isoformat()

        per_user = defaultdict(int)
        active = set()
        for day, users in by_day.items():
            # ISO date strings compare correctly as strings.
            if start_day <= day < end_day:
                for u in users:
                    per_user[u] += 1
                    active.add(u)

        three_plus = sum(1 for _, n in per_user.items() if n >= 3)
        pct = (three_plus / len(active) * 100) if active else 0.0
        out.append({
            "window_start": start_day,
            "window_end": end_day,
            "active_users": len(active),
            "three_plus_days": three_plus,
            "pct": round(pct, 1),
            "has_data": len(active) > 0,
        })
    return out


def compute_measure_stats(conn):
    # Sprint 3 -- numbers behind Measures 2 and 3. Extended in Sprint 5 so
    # Measure 2 respects its actual definition: corrected *within five
    # minutes*, not merely corrected eventually.
    total_violations = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'violation'"
    ).fetchone()[0]
    resolved_rows = conn.execute(
        "SELECT timestamp, resolved_at FROM events "
        "WHERE event_type = 'violation' AND resolved_at IS NOT NULL"
    ).fetchall()

    self_corrected_5min = 0
    for r in resolved_rows:
        try:
            t1 = datetime.fromisoformat(r["timestamp"])
            t2 = datetime.fromisoformat(r["resolved_at"])
            if (t2 - t1).total_seconds() <= SELF_CORRECTION_WINDOW_SECONDS:
                self_corrected_5min += 1
        except ValueError:
            pass

    corrections = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'correction'"
    ).fetchone()[0]
    rate = (self_corrected_5min / total_violations * 100) if total_violations else 0.0

    # Crude stuck-card lag: average hours between a card landing in a
    # column and its next move out of it, computed from consecutive
    # move rows in the event log.
    rows = conn.execute(
        "SELECT card_id, to_col, timestamp FROM events "
        "WHERE event_type IN ('move', 'violation') "
        "ORDER BY card_id, id"
    ).fetchall()
    lags = []
    last = {}
    for r in rows:
        cid = r["card_id"]
        if cid in last:
            try:
                prev_col, prev_ts = last[cid]
                if prev_col == r["to_col"] and prev_col != "done":
                    t1 = datetime.fromisoformat(prev_ts)
                    t2 = datetime.fromisoformat(r["timestamp"])
                    lags.append((t2 - t1).total_seconds() / 3600.0)
            except ValueError:
                pass
        last[cid] = (r["to_col"], r["timestamp"])
    avg_lag = sum(lags) / len(lags) if lags else 0.0

    return {
        "total_violations": total_violations,
        "self_corrected": self_corrected_5min,
        "self_corrected_any": len(resolved_rows),
        "corrections_logged": corrections,
        "self_correction_rate_pct": round(rate, 1),
        "avg_lag_hours": round(avg_lag, 2),
        "lag_samples": len(lags),
    }


def compute_measure_summary(conn):
    # Sprint 5 -- one function that answers "are we meeting our measures?"
    # Used by the metrics page, the export, and the coordinator view, so
    # none of them can disagree about pass or fail.
    weekly = compute_dau_weekly(conn, weeks=4)
    latest = weekly[0] if weekly else None
    m1_pct = latest["pct"] if latest else 0.0
    m1_has_data = bool(latest and latest["has_data"])

    stats = compute_measure_stats(conn)

    baseline = get_baseline(conn, BASELINE_KEY)
    reduction = None
    if baseline and baseline > 0 and stats["lag_samples"] > 0:
        reduction = round(
            (baseline - stats["avg_lag_hours"]) / baseline * 100, 1
        )

    open_critical = conn.execute(
        "SELECT COUNT(*) FROM issues WHERE resolved = 0 AND severity = 'critical'"
    ).fetchone()[0]

    m1_pass = m1_has_data and m1_pct >= MEASURE_THRESHOLDS["dau_pct"]
    m2_pass = (stats["total_violations"] > 0
               and stats["self_correction_rate_pct"] >= MEASURE_THRESHOLDS["self_correction_pct"])
    m3_pass = (reduction is not None
               and reduction >= MEASURE_THRESHOLDS["lag_reduction_pct"])

    return {
        "m1": {
            "pct": m1_pct,
            "threshold": MEASURE_THRESHOLDS["dau_pct"],
            "pass": m1_pass,
            "has_data": m1_has_data,
            "window_start": latest["window_start"] if latest else None,
            "window_end": latest["window_end"] if latest else None,
            "active_users": latest["active_users"] if latest else 0,
            "three_plus_days": latest["three_plus_days"] if latest else 0,
        },
        "m2": {
            "pct": stats["self_correction_rate_pct"],
            "threshold": MEASURE_THRESHOLDS["self_correction_pct"],
            "pass": m2_pass,
            "has_data": stats["total_violations"] > 0,
            "violations": stats["total_violations"],
            "corrected_within_5min": stats["self_corrected"],
            "corrected_eventually": stats["self_corrected_any"],
            "window_seconds": SELF_CORRECTION_WINDOW_SECONDS,
        },
        "m3": {
            "pct": reduction,
            "threshold": MEASURE_THRESHOLDS["lag_reduction_pct"],
            "pass": m3_pass,
            "has_data": reduction is not None,
            "baseline_hours": baseline,
            "current_hours": stats["avg_lag_hours"],
            "samples": stats["lag_samples"],
        },
        "open_critical_issues": open_critical,
        "all_measures_pass": m1_pass and m2_pass and m3_pass,
        "ready_for_sponsor": m1_pass and m2_pass and m3_pass and open_critical == 0,
    }


# == Sprint 3 -- auth routes ==
@app.route("/login", methods=["GET", "POST"])
def login():
    # Renders the login form on GET; verifies credentials on POST.
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            # Sprint 3 -- a login row is what makes DAU computable.
            conn.execute(
                "INSERT INTO logins (user_id, timestamp) VALUES (?, ?)",
                (user["id"], datetime.now().astimezone().isoformat())
            )
            conn.commit()
            conn.close()
            return redirect(url_for("index"))
        conn.close()
        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# == Board routes ==
@app.route("/")
@login_required
def index():
    # Showcase the board with all cards grouped by column
    conn = get_db()
    user = current_user(conn)
    cards = conn.execute("SELECT * FROM cards").fetchall()
    counts = column_counts(conn)
    last = last_activity(conn)

    owners = {
        r["id"]: r["display_name"]
        for r in conn.execute("SELECT id, display_name FROM users").fetchall()
    }
    comment_counts = {
        r["card_id"]: r["n"] for r in conn.execute(
            "SELECT card_id, COUNT(*) AS n FROM comments GROUP BY card_id"
        ).fetchall()
    }

    card_view = []
    for c in cards:
        card_view.append({
            "id": c["id"],
            "title": c["title"],
            "col": c["col"],
            "stale": is_stale(c, last.get(c["id"])),
            "owner": owners.get(c["owner_id"]),
            "due_date": c["due_date"],
            "overdue": is_overdue(c["due_date"], c["col"]),
            "blocked": bool(c["blocked"]),
            "block_reason": c["block_reason"],
            "comments": comment_counts.get(c["id"], 0),
        })
    conn.close()
    return render_template(
        "index.html",
        cards=card_view,
        counts=counts,
        limits=WIP_LIMITS,
        stale_days=STALE_DAYS,
        user=user,
    )


@app.route("/move", methods=["POST"])
@login_required
def move():
    # Move a card to a target column, and log an event.
    # Sprint 5 -- a Done column now exists, so the destination is explicit.
    # If none is supplied, fall back to the old toggle so nothing that
    # still posts only a card_id breaks.
    data = request.get_json()
    card_id = data["card_id"]
    target = data.get("to_col")

    conn = get_db()
    user = current_user(conn)
    username = user["username"] if user else "anonymous"

    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        conn.close()
        return jsonify({"ok": False, "error": "card not found"}), 404

    if target not in WIP_LIMITS:
        target = "in_progress" if card["col"] == "todo" else "todo"

    # Nothing to do if the card is already where it was dropped.
    if target == card["col"]:
        counts = column_counts(conn)
        conn.close()
        return jsonify({"ok": True, "new_col": target, "counts": counts, "warning": None})

    conn.execute("UPDATE cards SET col = ? WHERE id = ?", (target, card_id))
    event_type = check_wip_and_log(conn, card_id, username, card["col"], target)
    conn.commit()

    counts = column_counts(conn)
    conn.close()

    warning = None
    dest_limit = WIP_LIMITS.get(target)
    if dest_limit is not None and counts.get(target, 0) > dest_limit:
        warning = (
            f"WIP limit exceeded: {target} now has "
            f"{counts.get(target, 0)} cards (limit {dest_limit})."
        )

    socketio.emit("card_moved", {
        "card_id": card_id,
        "new_col": target,
        "counts": counts,
    })

    return jsonify({
        "ok": True,
        "new_col": target,
        "counts": counts,
        "warning": warning,
    })


@app.route("/add", methods=["POST"])
@login_required
def add():
    # Sprint 2b -- minimal add-card endpoint.
    # Sprint 3 -- accepts an owner by id or by username, plus a due date.
    data = request.get_json()
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400

    conn = get_db()
    team_id = current_team_id(conn)

    # Sprint 4 -- resolve the owner in one place, accepting either form,
    # and silently fall back to unassigned rather than crashing on a typo.
    owner_id = data.get("owner_id") or None
    owner_username = (data.get("owner_username") or "").strip()
    if not owner_id and owner_username:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (owner_username,)
        ).fetchone()
        owner_id = row["id"] if row else None

    due_date = data.get("due_date") or None

    conn.execute(
        "INSERT INTO cards (title, col, created_at, owner_id, due_date, team_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (title, "todo", datetime.now().astimezone().isoformat(),
         owner_id, due_date, team_id)
    )
    conn.commit()
    counts = column_counts(conn)
    conn.close()

    socketio.emit("card_added", {"title": title, "counts": counts})
    return jsonify({"ok": True, "counts": counts})


# == Sprint 3 -- card detail (comments + blocking) ==
@app.route("/card/<int:card_id>")
@login_required
def card_detail(card_id):
    # Detail page: card info, blocked state, and full comment thread.
    conn = get_db()
    user = current_user(conn)
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        conn.close()
        return "Card not found", 404

    owner = None
    if card["owner_id"]:
        owner = conn.execute(
            "SELECT display_name FROM users WHERE id = ?", (card["owner_id"],)
        ).fetchone()

    comments = conn.execute(
        "SELECT c.*, u.display_name AS author "
        "FROM comments c JOIN users u ON u.id = c.user_id "
        "WHERE c.card_id = ? ORDER BY c.id",
        (card_id,)
    ).fetchall()
    conn.close()

    return render_template(
        "card.html",
        card=card,
        owner=owner["display_name"] if owner else None,
        comments=comments,
        user=user,
    )


@app.route("/block", methods=["POST"])
@login_required
def block():
    # Sprint 3 -- mark a card blocked with a reason, and log the change.
    data = request.get_json()
    card_id = data["card_id"]
    reason = (data.get("reason") or "").strip()

    conn = get_db()
    user = current_user(conn)
    username = user["username"] if user else "anonymous"
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()

    conn.execute(
        "UPDATE cards SET blocked = 1, block_reason = ? WHERE id = ?",
        (reason, card_id)
    )
    # Log as a non-moving event: from/to both point at the current column.
    log_event(conn, card_id, username, card["col"], card["col"], "block")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/unblock", methods=["POST"])
@login_required
def unblock():
    # Sprint 3 -- clear a card's blocked state.
    data = request.get_json()
    card_id = data["card_id"]

    conn = get_db()
    user = current_user(conn)
    username = user["username"] if user else "anonymous"
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()

    conn.execute(
        "UPDATE cards SET blocked = 0, block_reason = NULL WHERE id = ?",
        (card_id,)
    )
    log_event(conn, card_id, username, card["col"], card["col"], "unblock")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/comment", methods=["POST"])
@login_required
def comment():
    # Sprint 3 -- attach a plain-text comment to a card.
    data = request.get_json()
    card_id = data["card_id"]
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "body required"}), 400

    conn = get_db()
    user = current_user(conn)
    conn.execute(
        "INSERT INTO comments (card_id, user_id, body, timestamp) "
        "VALUES (?, ?, ?, ?)",
        (card_id, user["id"], body, datetime.now().astimezone().isoformat())
    )
    conn.commit()
    conn.close()

    socketio.emit("comment_added", {"card_id": card_id})
    return jsonify({"ok": True})


# == Sprint 3 -- personalized view + export ==
@app.route("/my")
@login_required
def my_view():
    # Personalized dashboard: my cards, overdue, blocked, and progress.
    conn = get_db()
    user = current_user(conn)

    my_cards = conn.execute(
        "SELECT * FROM cards WHERE owner_id = ? ORDER BY col, id",
        (user["id"],)
    ).fetchall()

    # Sprint 4 -- overdue now uses the shared date-only check.
    overdue = [c for c in my_cards if is_overdue(c["due_date"], c["col"])]
    blocked = [c for c in my_cards if c["blocked"]]

    total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE col = 'done'"
    ).fetchone()[0]
    progress = round(done / total * 100, 0) if total else 0

    conn.close()
    return render_template(
        "my.html",
        user=user,
        my_cards=my_cards,
        overdue=overdue,
        blocked=blocked,
        progress=progress,
        total=total,
        done=done,
    )


@app.route("/export")
@login_required
def export():
    # Sprint 3 -- weekly export.
    # Sprint 5 -- now carries all three measures, their thresholds, their
    # pass/fail status, the baseline comparison, and the open critical
    # issue count. This is the artifact the sponsor review is built on.
    conn = get_db()
    dau = compute_dau(conn, days=14)
    weekly = compute_dau_weekly(conn, weeks=4)
    summary = compute_measure_summary(conn)
    issues = conn.execute(
        "SELECT title, severity, resolved FROM issues ORDER BY id"
    ).fetchall()
    conn.close()

    buf = StringIO()
    buf.write("measure,current,threshold,status\n")
    buf.write(
        f"Measure 1 - DAU 3+ days/week (%),{summary['m1']['pct']},"
        f"{summary['m1']['threshold']},{'PASS' if summary['m1']['pass'] else 'FAIL'}\n"
    )
    buf.write(
        f"Measure 2 - self-corrected within 5 min (%),{summary['m2']['pct']},"
        f"{summary['m2']['threshold']},{'PASS' if summary['m2']['pass'] else 'FAIL'}\n"
    )
    m3 = summary["m3"]["pct"]
    buf.write(
        f"Measure 3 - lag reduction vs baseline (%),{m3 if m3 is not None else 'n/a'},"
        f"{summary['m3']['threshold']},{'PASS' if summary['m3']['pass'] else 'FAIL'}\n"
    )
    buf.write(
        f"Open critical issues,{summary['open_critical_issues']},0,"
        f"{'PASS' if summary['open_critical_issues'] == 0 else 'FAIL'}\n"
    )
    buf.write(
        f"Ready for sponsor review,,,{summary['ready_for_sponsor']}\n"
    )
    buf.write("\n")

    buf.write("detail,value\n")
    buf.write(f"baseline_avg_lag_hours,{summary['m3']['baseline_hours'] or 'not recorded'}\n")
    buf.write(f"current_avg_lag_hours,{summary['m3']['current_hours']}\n")
    buf.write(f"lag_samples,{summary['m3']['samples']}\n")
    buf.write(f"violations,{summary['m2']['violations']}\n")
    buf.write(f"corrected_within_5min,{summary['m2']['corrected_within_5min']}\n")
    buf.write(f"corrected_eventually,{summary['m2']['corrected_eventually']}\n")
    buf.write("\n")

    buf.write("week_start,week_end,active_users,three_plus_days,dau_pct\n")
    for w in weekly:
        buf.write(
            f"{w['window_start']},{w['window_end']},{w['active_users']},"
            f"{w['three_plus_days']},{w['pct']}\n"
        )
    buf.write("\n")

    buf.write("day,dau\n")
    for row in dau:
        buf.write(f"{row['day']},{row['users']}\n")
    buf.write("\n")

    buf.write("issue,severity,resolved\n")
    for r in issues:
        buf.write(f"\"{r['title'].replace(chr(34), chr(39))}\",{r['severity']},{bool(r['resolved'])}\n")

    response = app.response_class(buf.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=trace_export.csv"
    return response


# == Sprint 4 -- coordinator view ==
@app.route("/coordinator")
@login_required
def coordinator():
    # One screen that answers: which cards are stuck, who is carrying too
    # much, and how is the team doing overall.
    conn = get_db()
    user = current_user(conn)
    if not user or user["role"] != "coordinator":
        conn.close()
        return "Coordinator access only", 403

    owners = {
        r["id"]: r["display_name"]
        for r in conn.execute("SELECT id, display_name FROM users").fetchall()
    }

    all_cards = conn.execute("SELECT * FROM cards").fetchall()
    last = last_activity(conn)

    # -- Stalled cards: idle longer than the staleness threshold --
    stalled = []
    for c in all_cards:
        days = idle_days(c, last.get(c["id"]))
        if is_stale(c, last.get(c["id"])):
            stalled.append({
                "id": c["id"],
                "title": c["title"],
                "col": c["col"],
                "owner": owners.get(c["owner_id"]) or "Unassigned",
                "blocked": bool(c["blocked"]),
                "idle_days": round(days, 1),
            })
    stalled.sort(key=lambda r: r["idle_days"], reverse=True)

    # -- Overcommitted members: more active cards than MEMBER_WIP_LIMIT --
    rows = conn.execute(
        "SELECT owner_id, COUNT(*) AS n FROM cards "
        "WHERE col != 'done' AND owner_id IS NOT NULL "
        "GROUP BY owner_id"
    ).fetchall()
    overcommitted = []
    for r in rows:
        if r["n"] > MEMBER_WIP_LIMIT:
            overcommitted.append({
                "owner": owners.get(r["owner_id"], "Unknown"),
                "active": r["n"],
                "over_by": r["n"] - MEMBER_WIP_LIMIT,
            })
    overcommitted.sort(key=lambda r: r["active"], reverse=True)

    # -- Team-level stats --
    counts = column_counts(conn)
    total = len(all_cards)
    done = counts.get("done", 0)
    unassigned = sum(1 for c in all_cards if not c["owner_id"] and c["col"] != "done")
    blocked_count = sum(1 for c in all_cards if c["blocked"])
    overdue_count = sum(1 for c in all_cards if is_overdue(c["due_date"], c["col"]))
    stats = compute_measure_stats(conn)

    # Sprint 5 -- the measure gates belong on this screen too, since this
    # is the screen a coordinator opens.
    summary = compute_measure_summary(conn)

    team_stats = {
        "total_cards": total,
        "done": done,
        "done_pct": round(done / total * 100, 0) if total else 0,
        "in_todo": counts.get("todo", 0),
        "in_progress": counts.get("in_progress", 0),
        "stalled": len(stalled),
        "blocked": blocked_count,
        "overdue": overdue_count,
        "unassigned": unassigned,
        "wip_limit": WIP_LIMITS.get("in_progress"),
        "member_limit": MEMBER_WIP_LIMIT,
        "violations": stats["total_violations"],
        "self_correction_rate": stats["self_correction_rate_pct"],
        "avg_lag_hours": stats["avg_lag_hours"],
    }

    conn.close()
    return render_template(
        "coordinator.html",
        user=user,
        stalled=stalled,
        overcommitted=overcommitted,
        stats=team_stats,
        summary=summary,
        stale_days=STALE_DAYS,
    )


# == Sprint 5 -- metrics dashboard ==
@app.route("/metrics")
@login_required
def metrics():
    # The screen the sprint exists for: three measures, their thresholds,
    # and whether we are ready for the sponsor.
    conn = get_db()
    user = current_user(conn)
    if not user or user["role"] != "coordinator":
        conn.close()
        return "Coordinator access only", 403

    weekly = compute_dau_weekly(conn, weeks=4)
    summary = compute_measure_summary(conn)
    issues = conn.execute(
        "SELECT * FROM issues ORDER BY resolved, id DESC"
    ).fetchall()
    conn.close()

    return render_template(
        "metrics.html",
        user=user,
        summary=summary,
        weekly=weekly,
        issues=issues,
        baseline_key=BASELINE_KEY,
    )


@app.route("/baseline", methods=["POST"])
@login_required
def baseline():
    # Sprint 5 -- record the Sprint 1 lag baseline. Without this, Measure 3
    # cannot be computed, so the route exists rather than a magic number.
    conn = get_db()
    user = current_user(conn)
    if not user or user["role"] != "coordinator":
        conn.close()
        return "Coordinator access only", 403

    raw = (request.form.get("value") or "").strip()
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        conn.close()
        return redirect(url_for("metrics"))

    set_baseline(conn, BASELINE_KEY, value)
    conn.commit()
    conn.close()
    return redirect(url_for("metrics"))


@app.route("/issue", methods=["POST"])
@login_required
def issue():
    # Sprint 5 -- log a critical issue found during adoption. "No
    # unresolved critical bugs" is an exit criterion, so it is a row.
    conn = get_db()
    user = current_user(conn)
    if not user or user["role"] != "coordinator":
        conn.close()
        return "Coordinator access only", 403

    title = (request.form.get("title") or "").strip()
    severity = request.form.get("severity") or "critical"
    if title:
        conn.execute(
            "INSERT INTO issues (title, severity, resolved, created_at) "
            "VALUES (?, ?, 0, ?)",
            (title, severity, datetime.now().astimezone().isoformat())
        )
        conn.commit()
    conn.close()
    return redirect(url_for("metrics"))


@app.route("/issue/<int:issue_id>/resolve", methods=["POST"])
@login_required
def resolve_issue(issue_id):
    # Sprint 5 -- close an issue once it has been fixed.
    conn = get_db()
    user = current_user(conn)
    if not user or user["role"] != "coordinator":
        conn.close()
        return "Coordinator access only", 403

    conn.execute(
        "UPDATE issues SET resolved = 1, resolved_at = ? WHERE id = ?",
        (datetime.now().astimezone().isoformat(), issue_id)
    )
    conn.commit()
    conn.close()
    return redirect(url_for("metrics"))


init_db()
if __name__ == "__main__":
    socketio.run(app, debug=True)