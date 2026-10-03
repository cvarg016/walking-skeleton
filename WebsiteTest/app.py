# == Walking Skeleton ==
# General proof of concept-- the card moves and the event logs from a click
# Sprint 2 additions: WIP limits with visible counters, staleness indicators, and
# automatic warnings + logging for limit violations and corrections.

import sqlite3
import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))
socketio = SocketIO(app, cors_allowed_origins="*")

DB = "skeleton.db"

# WIP limit per column
WIP_LIMITS = {
    "todo": None,
    "in_progress": 3,
}

# A card is stale if it hasn't moved or been touched in
# more than STALENESS_THRESHOLD and is not Done
STALENESS_THRESHOLD = timedelta(days=3)
STALE_DAYS = STALENESS_THRESHOLD.days


def get_db():
    # Open a connection to the SQLite file
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # Create the two tables if they don't exist, and seed one card
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            col TEXT NOT NULL DEFAULT 'todo',
            created_at TEXT
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
    """)

    # Add columns to any pre-Sprint-2 database.
    # Swallow the error if the column is already there.
    for stmt in (
        "ALTER TABLE cards ADD COLUMN created_at TEXT",
        "ALTER TABLE events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'move'",
        "ALTER TABLE events ADD COLUMN resolved_at TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Backfill created_at on any card that predates the migration so the
    # staleness calculation has something to work with.
    conn.execute(
        "UPDATE cards SET created_at = ? WHERE created_at IS NULL",
        (datetime.now().astimezone().isoformat(),)
    )

    # Seed one card if the table is empty
    if conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0:
        conn.execute("INSERT INTO cards (title, col) VALUES (?, ?)",
                     ("First card", "todo"))
        conn.commit()
    conn.close()


def column_counts(conn):
    # Used by the visible WIP counters on each column header.
    rows = conn.execute(
        "SELECT col, COUNT(*) AS n FROM cards GROUP BY col"
    ).fetchall()
    return {r["col"]: r["n"] for r in rows}


def last_activity(conn):
    # Per-card last activity for staleness
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
    # event_type is one of:
    #   'move'       normal card movement
    #   'violation'  move that pushed a column over its WIP limit
    #   'correction' subsequent move that brought the column back under
    conn.execute(
        "INSERT INTO events (card_id, user, from_col, to_col, timestamp, event_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (card_id, user, from_col, to_col,
         datetime.now().astimezone().isoformat(), event_type)
    )


def check_wip_and_log(conn, card_id, user, from_col, to_col):
    # Called AFTER the card's column has
    # already been updated, so column counts include the move we just made.
    now = datetime.now().astimezone().isoformat()

    # -- Violation check --
    dest_limit = WIP_LIMITS.get(to_col)
    dest_count = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE col = ?", (to_col,)
    ).fetchone()[0]
    is_violation = dest_limit is not None and dest_count > dest_limit

    event_type = "violation" if is_violation else "move"
    log_event(conn, card_id, user, from_col, to_col, event_type)

    # -- Correction check --
    src_limit = WIP_LIMITS.get(from_col)
    if src_limit is not None:
        src_count = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE col = ?", (from_col,)
        ).fetchone()[0]
        if src_count <= src_limit:
            # Any still-open violations on this column are now corrected.
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


@app.route("/")
def index():
    # Showcase the board with all cards grouped by column
    conn = get_db()
    cards = conn.execute("SELECT * FROM cards").fetchall()
    counts = column_counts(conn)
    last = last_activity(conn)
    # Decorate each card with its stale flag for the template.
    card_view = []
    for c in cards:
        card_view.append({
            "id": c["id"],
            "title": c["title"],
            "col": c["col"],
            "stale": is_stale(c, last.get(c["id"])),
        })
    conn.close()
    return render_template(
        "index.html",
        cards=card_view,
        counts=counts,
        limits=WIP_LIMITS,
        stale_days=STALE_DAYS,
    )


@app.route("/move", methods=["POST"])
def move():
    # Move a card from one column to the other, and log an event.
    # Also detect WIP violations/corrections and return a
    # warning message the UI can surface.
    data = request.get_json()
    card_id = data["card_id"]
    user = data.get("user", "test_user")  # Hardcoded for the skeleton

    conn = get_db()
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()

    # Decide the new column
    new_col = "in_progress" if card["col"] == "todo" else "todo"

    # Update the card
    conn.execute("UPDATE cards SET col = ? WHERE id = ?", (new_col, card_id))

    # Log the move and evaluate WIP rules
    event_type = check_wip_and_log(conn, card_id, user, card["col"], new_col)

    conn.commit()

    # Grab fresh counts before closing so the UI can update
    # its visible WIP counters.
    counts = column_counts(conn)
    conn.close()

    # If the destination column is still over its limit, the
    # warning stands. Otherwise it clears
    warning = None
    dest_limit = WIP_LIMITS.get(new_col)
    if dest_limit is not None and counts.get(new_col, 0) > dest_limit:
        warning = (
            f"WIP limit exceeded: {new_col} now has "
            f"{counts.get(new_col, 0)} cards (limit {dest_limit})."
        )

    # Broadcast card position + counts to every connected
    # client so all open boards stay in sync.
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
def add():
    # Minimal add-card endpoint so the board isn't a dead end
    data = request.get_json()
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO cards (title, col, created_at) VALUES (?, ?, ?)",
        (title, "todo", datetime.now().astimezone().isoformat())
    )
    conn.commit()
    counts = column_counts(conn)
    conn.close()

    # Lets every open board update its counters live
    socketio.emit("card_added", {"title": title, "counts": counts})

    return jsonify({"ok": True, "counts": counts})


init_db()
if __name__ == "__main__":
    socketio.run(app, debug=True)
