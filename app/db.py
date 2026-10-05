"""Base de données SQLite (module standard de Python, rien à installer).

Le schéma est décrit dans SCHEMA. Au démarrage, `migrate()` crée les tables
manquantes et ajoute les colonnes manquantes : quand on ajoute un module plus
tard, il suffit de compléter SCHEMA, sans commande de migration à lancer.
"""
import os
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

# ---------------------------------------------------------------------------
# Schéma : table -> liste de (colonne, définition SQL)
# La première colonne de chaque table est la clé primaire.
# ---------------------------------------------------------------------------
SCHEMA = {
    "users": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("username", "TEXT NOT NULL"),
        ("full_name", "TEXT DEFAULT ''"),
        ("email", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''"),
        ("role", "TEXT NOT NULL DEFAULT 'lecteur'"),
        ("password_hash", "TEXT NOT NULL"),
        ("must_change_password", "INTEGER NOT NULL DEFAULT 1"),
        ("setup_done", "INTEGER NOT NULL DEFAULT 0"),
        ("security_question", "TEXT DEFAULT ''"),
        ("security_answer_hash", "TEXT DEFAULT ''"),
        ("perms", "TEXT DEFAULT ''"),
        ("theme", "TEXT NOT NULL DEFAULT 'auto'"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
        ("deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("locked_until", "TEXT DEFAULT ''"),
        ("reset_code_hash", "TEXT DEFAULT ''"),
        ("reset_code_expires", "TEXT DEFAULT ''"),
        ("created_at", "TEXT DEFAULT ''"),
        ("last_login", "TEXT DEFAULT ''"),
    ],
    "activity_log": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("user_id", "INTEGER"),
        ("username", "TEXT DEFAULT ''"),
        ("action", "TEXT NOT NULL"),
        ("details", "TEXT DEFAULT ''"),
        ("ip", "TEXT DEFAULT ''"),
        ("created_at", "TEXT NOT NULL"),
    ],
    "settings": [
        ("key", "TEXT PRIMARY KEY"),
        ("value", "TEXT DEFAULT ''"),
    ],
}

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS ix_log_created ON activity_log(created_at)",
]

DEFAULT_SUPER_ADMIN = {"username": "super-adm", "password": "Ampandrana"}


def now_utc():
    """Date/heure UTC au format texte, triable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def db_path():
    return current_app.config["DATABASE"]


def connect(path):
    conn = sqlite3.connect(path, timeout=30, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(db_path())
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=()):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    last_id = cur.lastrowid
    cur.close()
    return last_id


# ---------------------------------------------------------------------------
# Migration automatique
# ---------------------------------------------------------------------------
def _add_column_definition(definition):
    """SQLite n'accepte pas PRIMARY KEY / UNIQUE dans ALTER TABLE ADD COLUMN."""
    return definition.replace("PRIMARY KEY", "").replace("AUTOINCREMENT", "").replace("UNIQUE", "")


def migrate(conn):
    for table, columns in SCHEMA.items():
        cols_sql = ", ".join(f'"{name}" {definition}' for name, definition in columns)
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({cols_sql})')
        existing = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {_add_column_definition(definition)}')
    for sql in INDEXES:
        conn.execute(sql)
    conn.commit()


def ensure_super_admin(conn):
    """Garantit qu'un compte Super-admin existe toujours."""
    from werkzeug.security import generate_password_hash

    row = conn.execute(
        "SELECT id FROM users WHERE role = 'super_admin' AND deleted = 0 LIMIT 1"
    ).fetchone()
    if row:
        return
    username = DEFAULT_SUPER_ADMIN["username"]
    taken = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if taken:  # nom déjà utilisé par un compte supprimé : on le libère
        conn.execute(
            "UPDATE users SET username = username || '#' || id WHERE id = ?", (taken["id"],)
        )
    conn.execute(
        """INSERT INTO users (username, full_name, role, password_hash,
               must_change_password, setup_done, created_at)
           VALUES (?, 'Super administrateur', 'super_admin', ?, 1, 0, ?)""",
        (username, generate_password_hash(DEFAULT_SUPER_ADMIN["password"]), now_utc()),
    )
    conn.commit()


def init_app(app):
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    with app.app_context():
        conn = connect(app.config["DATABASE"])
        try:
            migrate(conn)
            ensure_super_admin(conn)
        finally:
            conn.close()
    app.teardown_appcontext(close_db)


def is_valid_database(path):
    """Vérifie qu'un fichier est bien une sauvegarde de ce logiciel."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        return check == "ok" and {"users", "settings"} <= tables
    except sqlite3.DatabaseError:
        return False
