# == Walking Skeleton ==
# General proof of concept-- the card moves and the event logs from a click
# Sprint 2: WIP limits with visible counters, staleness indicators, and
#           automatic warnings + logging for limit violations and corrections.
# Sprint 2b: minimal add-card endpoint so the board isn't a dead end.
# Sprint 3: user login + roster, card blocking + comments, personalized
#           view (overdue + sprint progress), and a weekly DAU export.

import sqlite3
import os
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
}

# Sprint 2 -- staleness threshold for idle cards
STALENESS_THRESHOLD = timedelta(days=3)
STALE_DAYS = STALENESS_THRESHOLD.days

# Sprint 3 -- sprint length used by the personalized view's progress bar.
SPRINT_LENGTH_DAYS = 14


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
            team_id INTEGER
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

    # Backfill created_at on any card that predates the migration
    conn.execute(
        "UPDATE cards SET created_at = ? WHERE created_at IS NULL",
        (datetime.now().astimezone().isoformat(),)
    )

    # Sprint 3 -- seed a demo team and three users on first run.
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0:
        conn.execute("INSERT INTO teams (name) VALUES (?)", ("Trace Team",))
    team_id = conn.execute("SELECT id FROM teams LIMIT 1").fetchone()[0]

    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        demo_users = [
            ("alice", "password", "Alice"),
            ("bob",   "password", "Bob"),
            ("carol", "password", "Carol"),
        ]
        for username, pw, display in demo_users:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, team_id) "
                "VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(pw), display, team_id)
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


def is_stale(card, last_ts):
    # Sprint 2 -- a card is stale if it isn't Done and its last activity
    # is older than STALENESS_THRESHOLD.
    if card["col"] == "done":
        return False
    if not last_ts:
        return False
    try:
        ts = datetime.fromisoformat(last_ts)
    except ValueError:
        return False
    now = datetime.now().astimezone()
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return (now - ts) > STALENESS_THRESHOLD


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


def compute_measure_stats(conn):
    # Sprint 3 -- gather the numbers behind Measures 2 and 3 for export.
    total_violations = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'violation'"
    ).fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM events "
        "WHERE event_type = 'violation' AND resolved_at IS NOT NULL"
    ).fetchone()[0]
    corrections = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'correction'"
    ).fetchone()[0]
    rate = (resolved / total_violations * 100) if total_violations else 0.0

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
        "self_corrected": resolved,
        "corrections_logged": corrections,
        "self_correction_rate_pct": round(rate, 1),
        "avg_lag_hours": round(avg_lag, 2),
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

    # Sprint 3 -- resolve owner names and comment counts so cards can
    # display them without extra lookups in the template.
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
    # Move a card from one column to the other, and log an event.
    data = request.get_json()
    card_id = data["card_id"]

    conn = get_db()
    user = current_user(conn)
    username = user["username"] if user else "anonymous"

    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    new_col = "in_progress" if card["col"] == "todo" else "todo"

    conn.execute("UPDATE cards SET col = ? WHERE id = ?", (new_col, card_id))
    event_type = check_wip_and_log(conn, card_id, username, card["col"], new_col)
    conn.commit()

    counts = column_counts(conn)
    conn.close()

    warning = None
    dest_limit = WIP_LIMITS.get(new_col)
    if dest_limit is not None and counts.get(new_col, 0) > dest_limit:
        warning = (
            f"WIP limit exceeded: {new_col} now has "
            f"{counts.get(new_col, 0)} cards (limit {dest_limit})."
        )

    socketio.emit("card_moved", {
        "card_id": card_id,
        "new_col": new_col,
        "counts": counts,
    })

    return jsonify({
        "ok": True,
        "new_col": new_col,
        "counts": counts,
        "warning": warning,
    })


@app.route("/add", methods=["POST"])
@login_required
def add():
    # Sprint 2b -- minimal add-card endpoint.
    # Sprint 3 -- accepts optional owner_id and due_date.
    data = request.get_json()
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400

    owner_id = data.get("owner_id") or None
    due_date = data.get("due_date") or None

    conn = get_db()
    team_id = current_team_id(conn)
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

    now_iso = datetime.now().astimezone().isoformat()
    overdue = [
        c for c in my_cards
        if c["due_date"] and c["due_date"] < now_iso and c["col"] != "done"
    ]
    blocked = [c for c in my_cards if c["blocked"]]

    # Sprint progress: done cards over total, across the team.
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
    # Sprint 3 -- weekly export. CSV with DAU per day plus the numbers
    # behind Measures 2 and 3.
    conn = get_db()
    dau = compute_dau(conn, days=14)
    stats = compute_measure_stats(conn)
    conn.close()

    buf = StringIO()
    buf.write("metric,value\n")
    buf.write(f"total_violations,{stats['total_violations']}\n")
    buf.write(f"self_corrected,{stats['self_corrected']}\n")
    buf.write(f"corrections_logged,{stats['corrections_logged']}\n")
    buf.write(f"self_correction_rate_pct,{stats['self_correction_rate_pct']}\n")
    buf.write(f"avg_lag_hours,{stats['avg_lag_hours']}\n")
    buf.write("\n")
    buf.write("day,dau\n")
    for row in dau:
        buf.write(f"{row['day']},{row['users']}\n")

    response = app.response_class(buf.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=trace_export.csv"
    return response


init_db()
if __name__ == "__main__":
    socketio.run(app, debug=True)
