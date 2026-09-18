# == Walking Skeleton ==
# General proof of concept-- the card moves and the event logs from a click

import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
DB = "skeleton.db"


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
            col TEXT NOT NULL DEFAULT 'todo'
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            card_id INTEGER NOT NULL,
            user TEXT NOT NULL,
            from_col TEXT NOT NULL,
            to_col TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
    """)
    # Seed one card if the table is empty
    if conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0:
        conn.execute("INSERT INTO cards (title, col) VALUES (?, ?)",
                     ("First card", "todo"))
        conn.commit()
    conn.close()


@app.route("/")
def index():
    # Showcase the board with all cards grouped by column
    conn = get_db()
    cards = conn.execute("SELECT * FROM cards").fetchall()
    conn.close()
    return render_template("index.html", cards=cards)


@app.route("/move", methods=["POST"])
def move():
    # Move a card from one column to the other, and log an event
    data = request.get_json()
    card_id = data["card_id"]
    user = data.get("user", "test_user")  # Hardcoded for the skeleton

    conn = get_db()
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()

    # Decide the new column
    new_col = "in_progress" if card["col"] == "todo" else "todo"

    # Update the card
    conn.execute("UPDATE cards SET col = ? WHERE id = ?", (new_col, card_id))

    # Log the event
    conn.execute(
        "INSERT INTO events (card_id, user, from_col, to_col, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (card_id, user, card["col"], new_col, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "new_col": new_col})


if __name__ == "__main__":
    init_db()
    app.run(debug=True)