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
        ("last_seen_message", "INTEGER NOT NULL DEFAULT 0"),
    ],
    # ----- Discussion entre associés et commentaires sur les saisies -----
    "messages": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("user_id", "INTEGER"),
        ("body", "TEXT NOT NULL"),
        ("ref_type", "TEXT DEFAULT ''"),
        ("ref_id", "INTEGER"),
        ("deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("auto", "INTEGER NOT NULL DEFAULT 0"),
        ("created_at", "TEXT NOT NULL"),
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
    # ----- Listes libres (catégories) -----
    "categories": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("kind", "TEXT NOT NULL DEFAULT 'matiere'"),
        ("name", "TEXT NOT NULL"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
    ],
    # ----- Comptes d'argent : caisses et propriétaires -----
    "accounts": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("name", "TEXT NOT NULL"),
        ("kind", "TEXT NOT NULL DEFAULT 'caisse'"),
        ("sort", "INTEGER NOT NULL DEFAULT 0"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
    ],
    "cash_movements": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("date", "TEXT NOT NULL"),
        ("account_id", "INTEGER"),
        ("amount", "REAL NOT NULL DEFAULT 0"),
        ("kind", "TEXT NOT NULL"),
        ("label", "TEXT DEFAULT ''"),
        ("ref_type", "TEXT DEFAULT ''"),
        ("ref_id", "INTEGER"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    # ----- Matières premières, fournisseurs, achats -----
    "materials": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("name", "TEXT NOT NULL"),
        ("category_id", "INTEGER"),
        ("unit", "TEXT NOT NULL DEFAULT 'kg'"),
        ("alert_threshold", "REAL NOT NULL DEFAULT 0"),
        ("avg_cost", "REAL NOT NULL DEFAULT 0"),
        ("notes", "TEXT DEFAULT ''"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "suppliers": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("name", "TEXT NOT NULL"),
        ("phone", "TEXT DEFAULT ''"),
        ("address", "TEXT DEFAULT ''"),
        ("notes", "TEXT DEFAULT ''"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "purchases": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("date", "TEXT NOT NULL"),
        ("supplier_id", "INTEGER"),
        ("reference", "TEXT DEFAULT ''"),
        ("transport_cost", "REAL NOT NULL DEFAULT 0"),
        ("total", "REAL NOT NULL DEFAULT 0"),
        ("paid", "REAL NOT NULL DEFAULT 0"),
        ("account_id", "INTEGER"),
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "purchase_lines": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("purchase_id", "INTEGER NOT NULL"),
        ("material_id", "INTEGER NOT NULL"),
        ("quantity", "REAL NOT NULL"),
        ("unit_price", "REAL NOT NULL DEFAULT 0"),
        ("total", "REAL NOT NULL DEFAULT 0"),
    ],
    "supplier_payments": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("supplier_id", "INTEGER NOT NULL"),
        ("date", "TEXT NOT NULL"),
        ("amount", "REAL NOT NULL"),
        ("account_id", "INTEGER"),
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "stock_moves": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("date", "TEXT NOT NULL"),
        ("material_id", "INTEGER NOT NULL"),
        ("quantity", "REAL NOT NULL"),
        ("unit_cost", "REAL NOT NULL DEFAULT 0"),
        ("kind", "TEXT NOT NULL"),
        ("ref_type", "TEXT DEFAULT ''"),
        ("ref_id", "INTEGER"),
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    # ----- Provenderie -----
    "formulas": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("name", "TEXT NOT NULL"),
        ("phase", "TEXT DEFAULT ''"),
        ("base_qty", "REAL NOT NULL DEFAULT 100"),
        ("alert_threshold", "REAL NOT NULL DEFAULT 0"),
        ("avg_cost", "REAL NOT NULL DEFAULT 0"),
        ("notes", "TEXT DEFAULT ''"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "formula_lines": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("formula_id", "INTEGER NOT NULL"),
        ("material_id", "INTEGER NOT NULL"),
        ("quantity", "REAL NOT NULL"),          # dans l'unité de stock de la matière (pour les calculs)
        ("input_qty", "REAL"),                   # quantité telle qu'écrite (ex. 500)
        ("input_unit", "TEXT DEFAULT ''"),       # unité telle qu'écrite (ex. g)
    ],
    "feed_programs": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("name", "TEXT NOT NULL"),
        ("notes", "TEXT DEFAULT ''"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "feed_program_weeks": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("program_id", "INTEGER NOT NULL"),
        ("week_from", "INTEGER NOT NULL"),
        ("week_to", "INTEGER NOT NULL"),
        ("formula_id", "INTEGER NOT NULL"),
        ("grams_per_bird", "REAL NOT NULL DEFAULT 0"),
    ],
    # ----- Lots de poules -----
    "lots": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("name", "TEXT NOT NULL"),
        ("arrival_date", "TEXT NOT NULL"),
        ("age_days_at_arrival", "INTEGER NOT NULL DEFAULT 0"),  # 0 = poussins d'un jour
        ("initial_count", "INTEGER NOT NULL DEFAULT 0"),
        ("breed", "TEXT DEFAULT ''"),
        ("building", "TEXT DEFAULT ''"),
        ("supplier", "TEXT DEFAULT ''"),
        ("chick_price", "REAL NOT NULL DEFAULT 0"),
        ("other_costs", "REAL NOT NULL DEFAULT 0"),
        ("paid", "REAL NOT NULL DEFAULT 0"),
        ("account_id", "INTEGER"),
        ("program_id", "INTEGER"),
        ("reform_week", "INTEGER NOT NULL DEFAULT 72"),
        ("laying_week", "INTEGER NOT NULL DEFAULT 18"),  # semaine où les poules commencent à pondre (variable)
        ("egg_price", "REAL NOT NULL DEFAULT 0"),
        ("status", "TEXT NOT NULL DEFAULT 'actif'"),
        ("end_date", "TEXT DEFAULT ''"),
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "lot_events": [  # morts, réformes, ventes, ajouts, corrections de comptage
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("lot_id", "INTEGER NOT NULL"),
        ("date", "TEXT NOT NULL"),
        ("kind", "TEXT NOT NULL"),
        ("quantity", "INTEGER NOT NULL"),  # signé : négatif = poules en moins
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "feedings": [  # provende donnée à un lot
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("lot_id", "INTEGER NOT NULL"),
        ("date", "TEXT NOT NULL"),
        ("formula_id", "INTEGER NOT NULL"),
        ("quantity", "REAL NOT NULL"),
        ("unit_cost", "REAL NOT NULL DEFAULT 0"),
        ("birds", "INTEGER NOT NULL DEFAULT 0"),
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "productions": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("date", "TEXT NOT NULL"),
        ("formula_id", "INTEGER NOT NULL"),
        ("quantity", "REAL NOT NULL"),
        ("cost_total", "REAL NOT NULL DEFAULT 0"),
        ("cost_per_kg", "REAL NOT NULL DEFAULT 0"),
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
    "production_lines": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("production_id", "INTEGER NOT NULL"),
        ("material_id", "INTEGER NOT NULL"),
        ("quantity", "REAL NOT NULL"),
        ("unit_cost", "REAL NOT NULL DEFAULT 0"),
    ],
    "feed_moves": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("date", "TEXT NOT NULL"),
        ("formula_id", "INTEGER NOT NULL"),
        ("quantity", "REAL NOT NULL"),
        ("unit_cost", "REAL NOT NULL DEFAULT 0"),
        ("kind", "TEXT NOT NULL"),
        ("ref_type", "TEXT DEFAULT ''"),
        ("ref_id", "INTEGER"),
        ("notes", "TEXT DEFAULT ''"),
        ("created_by", "INTEGER"),
        ("created_at", "TEXT DEFAULT ''"),
    ],
}

# Données « de saisie » effacées par « Effacer les données de test ».
# Les listes (matières, fournisseurs, formules, programmes, comptes,
# catégories), les utilisateurs et les paramètres sont gardés.
TRANSACTION_TABLES = [
    "purchases", "purchase_lines", "supplier_payments", "stock_moves",
    "productions", "production_lines", "feed_moves", "cash_movements",
    "lots", "lot_events", "feedings",
]

DEFAULT_ACCOUNTS = [
    ("Caisse de la ferme", "caisse", 1),
    ("Mobile money", "caisse", 2),
    ("Propriétaire 1", "proprietaire", 3),
    ("Propriétaire 2", "proprietaire", 4),
]
DEFAULT_CATEGORIES = ["Céréales", "Protéines", "Minéraux", "Prémix & vitamines", "Médicaments", "Emballages", "Divers"]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS ix_log_created ON activity_log(created_at)",
    "CREATE INDEX IF NOT EXISTS ix_stock_moves_mat ON stock_moves(material_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_stock_moves_ref ON stock_moves(ref_type, ref_id)",
    "CREATE INDEX IF NOT EXISTS ix_feed_moves_formula ON feed_moves(formula_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_feed_moves_ref ON feed_moves(ref_type, ref_id)",
    "CREATE INDEX IF NOT EXISTS ix_cash_ref ON cash_movements(ref_type, ref_id)",
    "CREATE INDEX IF NOT EXISTS ix_purchase_lines ON purchase_lines(purchase_id)",
    "CREATE INDEX IF NOT EXISTS ix_formula_lines ON formula_lines(formula_id)",
    "CREATE INDEX IF NOT EXISTS ix_messages_ref ON messages(ref_type, ref_id)",
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


BRAND_UPGRADE = [  # anciennes valeurs par défaut -> identité Androfia Farm (si jamais modifiées)
    ("company_name", "Ma Ferme Avicole", "Androfia Farm"),
    ("company_slogan", "Gestion de ferme de poules pondeuses", "Ferme de poules pondeuses"),
    ("primary_color", "#2f7d4f", "#1d6b35"),
    ("menu_color", "#16302b", "#0e3b20"),
]


def upgrade_brand(conn):
    for key, old, new in BRAND_UPGRADE:
        conn.execute("UPDATE settings SET value = ? WHERE key = ? AND value = ?", (new, key, old))
    conn.commit()


def seed_defaults(conn):
    """Listes de départ (une seule fois) : comptes d'argent et catégories.
    Les propriétaires peuvent ensuite tout renommer, ajouter ou retirer."""
    upgrade_brand(conn)
    done = conn.execute("SELECT value FROM settings WHERE key = 'seeded_v1'").fetchone()
    if done:
        return
    if not conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        conn.executemany("INSERT INTO accounts (name, kind, sort) VALUES (?, ?, ?)", DEFAULT_ACCOUNTS)
    if not conn.execute("SELECT 1 FROM categories WHERE kind = 'matiere' LIMIT 1").fetchone():
        conn.executemany("INSERT INTO categories (kind, name) VALUES ('matiere', ?)", [(c,) for c in DEFAULT_CATEGORIES])
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('seeded_v1', '1')")
    conn.commit()


class transaction:
    """Plusieurs écritures d'un coup : tout est enregistré, ou rien du tout."""

    def __enter__(self):
        self.conn = get_db()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        return False


def init_app(app):
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    with app.app_context():
        conn = connect(app.config["DATABASE"])
        try:
            migrate(conn)
            ensure_super_admin(conn)
            seed_defaults(conn)
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
