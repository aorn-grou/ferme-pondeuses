"""Version de démonstration (vitrine) : données d'exemple réalistes et remise à zéro automatique.

Construire la démo (une fois, dans la console) :
    FERME_DEMO=1 flask --app wsgi demo-build
Le site de démo (variable FERME_DEMO=1) :
  - affiche des boutons « Entrer comme … » sur la page de connexion,
  - montre un bandeau « Version de démonstration »,
  - bloque les actions dangereuses (utilisateurs, sauvegardes, paramètres, mots de passe),
  - se remet à zéro chaque jour : la copie de démo est rechargée et ses dates avancées jusqu'à aujourd'hui.

Les données sont créées en passant par les vraies pages du logiciel (comme un vrai utilisateur),
ce qui sert aussi de grand test automatique de tout le logiciel.
"""
import os
import random
import shutil
import sqlite3
from datetime import date as Date, timedelta

from werkzeug.security import generate_password_hash

DEMO_PASSWORD = "Demo2026"
DEMO_USERS = [
    # identifiant, nom affiché, rôle, ce qu'il montre
    ("proprietaire", "Rakoto (propriétaire)", "super_admin", "Tout voir et tout gérer"),
    ("associe", "Rasoa (associée)", "admin", "Associée : tout voir"),
    ("soigneur", "Jean (soigneur)", "elevage", "Lots, provende du jour, ramassage des œufs"),
    ("vendeur", "Hery (vendeur)", "ventes", "Ventes, clients, stock d'œufs"),
]
SNAPSHOT = "demo-snapshot.db"

# Composition des provendes (kg pour 100 kg)
FORMULAS = {
    "Démarrage": {"Maïs grain": 55, "Tourteau de soja": 35, "Son de riz": 5, "Poudre de coquillage": 3, "Prémix ponte": 2},
    "Croissance": {"Maïs grain": 60, "Tourteau de soja": 25, "Son de riz": 10, "Poudre de coquillage": 3, "Prémix ponte": 2},
    "Ponte": {"Maïs grain": 58, "Tourteau de soja": 22, "Son de riz": 8, "Poudre de coquillage": 10, "Prémix ponte": 2},
}
PROGRAM = [  # semaines du → au, provende, grammes par poule et par jour
    (1, 1, "Démarrage", 12), (2, 2, "Démarrage", 18), (3, 3, "Démarrage", 25), (4, 4, "Démarrage", 32),
    (5, 8, "Démarrage", 42), (9, 12, "Croissance", 58), (13, 16, "Croissance", 72), (17, 18, "Croissance", 90),
    (19, 80, "Ponte", 115),
]
MATERIALS = [  # nom, catégorie, unité, seuil d'alerte, prix d'achat, fournisseur
    ("Maïs grain", "Céréales", "kg", 600, 1300, "Rabe Grains"),
    ("Tourteau de soja", "Protéines", "kg", 250, 3200, "Provenderie Soa"),
    ("Son de riz", "Céréales", "kg", 150, 450, "Rabe Grains"),
    ("Poudre de coquillage", "Minéraux", "kg", 120, 500, "Coquillages Toliara"),
    ("Prémix ponte", "Prémix & vitamines", "kg", 15, 9500, "Provenderie Soa"),
]
CLIENTS = ["Épicerie Rasoa", "Hôtel Les Flamboyants", "Marché d'Analakely (Bao)", "Boulangerie Tiana", "Restaurant Chez Lova"]


def _laying_rate(age_week, laying_week, rng):
    """Courbe de ponte réaliste : montée rapide après le début, pic vers 27 semaines, puis lente baisse."""
    if age_week < laying_week:
        return 0.0
    w = age_week - laying_week
    if w < 8:
        base = 0.12 + (0.93 - 0.12) * (w / 8)
    else:
        base = 0.93 - 0.0035 * (w - 8)
    return max(0.0, min(0.97, base + rng.uniform(-0.025, 0.025)))


class _Client:
    """Navigateur simulé : passe par les vraies pages du logiciel."""

    def __init__(self, app):
        self.app = app
        self.c = app.test_client()
        with self.c.session_transaction() as s:
            s["_csrf"] = "demo"

    def post(self, url, data, ok=(302,)):
        with self.c.session_transaction() as s:  # la connexion renouvelle la session
            s.setdefault("_csrf", "demo")
            token = s["_csrf"]
        data = dict(data)
        data["_csrf"] = token
        res = self.c.post(url, data=data)
        if res.status_code not in ok:
            import re

            errors = re.findall(r'alert-error">.*?<span>(.*?)</span>', res.get_data(as_text=True), re.S)
            raise RuntimeError(f"Démo : {url} → {res.status_code} : {errors or res.get_data(as_text=True)[:800]}\n{data}")
        with self.c.session_transaction() as s:  # on ne garde pas les messages affichés (cookie trop gros)
            s.pop("_flashes", None)
        return res

    def get(self, url):
        return self.c.get(url)


def build(app, days=180, seed=7):
    """Remplit une base vide avec `days` jours d'activité d'exemple. Retourne un petit résumé."""
    from .db import get_db, query

    rng = random.Random(seed)
    today = Date.today()
    start = today - timedelta(days=days)
    br = _Client(app)

    # --- comptes ------------------------------------------------------------
    with app.app_context():
        conn = get_db()
        conn.execute("DELETE FROM users WHERE username != 'super-adm'")
        conn.execute("UPDATE users SET username = ?, full_name = ?, password_hash = ?, must_change_password = 0, "
                     "setup_done = 1, email = 'demo@example.com' WHERE username = 'super-adm'",
                     (DEMO_USERS[0][0], DEMO_USERS[0][1], generate_password_hash(DEMO_PASSWORD)))
        for username, name, role, _desc in DEMO_USERS[1:]:
            conn.execute("""INSERT INTO users (username, full_name, role, password_hash, must_change_password, setup_done, created_at)
                            VALUES (?, ?, ?, ?, 0, 1, datetime('now'))""",
                         (username, name, role, generate_password_hash(DEMO_PASSWORD)))
        conn.execute("UPDATE accounts SET name = 'Rakoto (propriétaire)' WHERE name = 'Propriétaire 1'")
        conn.execute("UPDATE accounts SET name = 'Rasoa (associée)' WHERE name = 'Propriétaire 2'")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('company_name', 'Androfia Farm')")
        conn.commit()
    br.post("/connexion", {"username": DEMO_USERS[0][0], "password": DEMO_PASSWORD})

    with app.app_context():
        caisse = query("SELECT id FROM accounts WHERE name = 'Caisse de la ferme'", one=True)["id"]
        proprio = query("SELECT id FROM accounts WHERE name LIKE 'Rakoto%'", one=True)["id"]
        mobile = query("SELECT id FROM accounts WHERE name = 'Mobile money'", one=True)["id"]

    # --- matières, formules, programme ---------------------------------------
    mat_ids, prices, suppliers = {}, {}, {}
    for name, cat, unit, threshold, price, supplier in MATERIALS:
        br.post("/matieres/matiere/nouvelle", {"name": name, "category_name": cat, "unit": unit,
                                               "alert_threshold": str(threshold), "notes": ""})
        with app.app_context():
            mat_ids[name] = query("SELECT id FROM materials WHERE name = ?", (name,), one=True)["id"]
        prices[name], suppliers[name] = price, supplier

    def buy(day, plan, pay_mode="tout"):
        """plan : {matière: kg} — un achat par fournisseur ; « partiel » = la moitié payée."""
        by_supplier = {}
        for name, kg in plan.items():
            by_supplier.setdefault(suppliers[name], []).append((name, kg))
        for supplier, items in by_supplier.items():
            unit_prices = [int(prices[n] * rng.uniform(0.96, 1.06)) for n, _ in items]
            goods = sum(k * p for (_, k), p in zip(items, unit_prices))
            br.post("/matieres/achats/nouveau", {
                "date": day.isoformat(), "supplier_name": supplier, "reference": "", "transport_cost": "0",
                "pay_mode": pay_mode, "paid": str(int(goods * 0.5)) if pay_mode == "partiel" else "",
                "account_id": str(caisse if rng.random() < 0.7 else proprio), "notes": "",
                "material_id": [str(mat_ids[n]) for n, _ in items], "quantity": [str(k) for _, k in items],
                "unit_price": [str(p) for p in unit_prices]})

    with app.app_context():  # argent de départ apporté par les propriétaires
        conn = get_db()
        conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, created_at)
                        VALUES (?, ?, 12000000, 'apport', 'Apport de départ des propriétaires', '', datetime('now'))""",
                     ((start - timedelta(days=1)).isoformat(), caisse))
        conn.commit()
    buy(start, {"Maïs grain": 3000, "Tourteau de soja": 1200, "Son de riz": 600, "Poudre de coquillage": 500, "Prémix ponte": 60})

    formula_ids = {}
    for fname, recipe in FORMULAS.items():
        br.post("/provenderie/formules/nouvelle", {
            "name": fname, "base_qty": "100", "notes": "",
            "material_id": [str(mat_ids[m]) for m in recipe], "quantity": [str(q) for q in recipe.values()],
            "qty_unit": ["kg"] * len(recipe)})
        with app.app_context():
            formula_ids[fname] = query("SELECT id FROM formulas WHERE name = ?", (fname,), one=True)["id"]
    br.post("/provenderie/programmes/nouveau", {
        "name": "Programme pondeuses", "notes": "Exemple : une provende par période, les grammes augmentent avec l'âge.",
        "week_from": [str(a) for a, _, _, _ in PROGRAM], "week_to": [str(b) for _, b, _, _ in PROGRAM],
        "formula_id": [str(formula_ids[f]) for _, _, f, _ in PROGRAM], "grams_per_bird": [str(g) for *_, g in PROGRAM]})
    with app.app_context():
        prog = query("SELECT id FROM feed_programs", one=True)["id"]

    # --- lots en escalier ----------------------------------------------------
    lots_plan = [  # nom, arrivée (jours avant aujourd'hui), poussins, bâtiment, début de ponte
        ("Lot A", 230, 600, "Bâtiment 1", 19),
        ("Lot B", 150, 500, "Bâtiment 2", 19),
        ("Lot C", 75, 400, "Bâtiment 3", 20),
        ("Lot D", 12, 300, "Bâtiment 3", 19),
    ]
    created = {}

    def create_lot(name, arrival, count, building, laying):
        br.post("/lots/nouveau", {
            "name": name, "arrival_date": arrival.isoformat(), "initial_count": str(count), "age_weeks": "", "age_days": "",
            "breed": "Isa Brown", "building": building, "program_id": str(prog), "reform_week": "80", "laying_week": str(laying),
            "egg_price": "500", "supplier": "Couvoir Rabe", "chick_price": "3500", "other_costs": "60000",
            "paid": "", "account_id": str(proprio), "notes": ""})
        with app.app_context():
            created[name] = query("SELECT * FROM lots WHERE name = ?", (name,), one=True)

    for name, ago, count, building, laying in lots_plan:
        if today - timedelta(days=ago) <= start:
            create_lot(name, today - timedelta(days=ago), count, building, laying)

    # --- jour après jour -----------------------------------------------------
    from .eggs import birds_on
    from .lots import lot_week, suggestion
    from .sales import egg_stock
    from .stock import feed_stock

    client_debt_sales = []
    last_buy = start
    for k in range(days):
        day = start + timedelta(days=k)
        d = day.isoformat()
        for name, ago, count, building, laying in lots_plan:
            if name not in created and today - timedelta(days=ago) == day:
                create_lot(name, day, count, building, laying)
        with app.app_context():
            lots = query("SELECT * FROM lots WHERE status = 'actif' AND arrival_date <= ?", (d,))
            sugs = {l["id"]: suggestion(l, d) for l in lots}
            need = {}
            for s in sugs.values():
                if s["formula_id"]:
                    need[s["formula_id"]] = need.get(s["formula_id"], 0) + s["kg"]
            stocks = {fid: feed_stock(fid) for fid in need}
            mat_stock = {n: query("SELECT COALESCE(SUM(quantity), 0) AS q FROM stock_moves WHERE material_id = ?",
                                  (i,), one=True)["q"] for n, i in mat_ids.items()}
        # achats quand les matières baissent (tous les ~12 jours au plus)
        low = {n: kg for n, kg in {"Maïs grain": 2500, "Tourteau de soja": 1000, "Son de riz": 500,
                                   "Poudre de coquillage": 500, "Prémix ponte": 50}.items()
               if mat_stock[n] < MATERIALS[[m[0] for m in MATERIALS].index(n)][3] * 1.6}
        if low and (day - last_buy).days >= 5:
            r = rng.random()
            buy(day, low, pay_mode="credit" if r < 0.15 else ("partiel" if r < 0.3 else "tout"))
            last_buy = day
        # fabrication : on fabrique quand il reste moins de 6 jours de provende
        for fid, kg_day in need.items():
            if stocks[fid] < kg_day * 6:
                qty = int(max(300, round(kg_day * 14 / 50) * 50))
                res = br.post("/provenderie/fabrication/nouvelle", {"formula_id": str(fid), "quantity": str(qty), "date": d,
                                                                     "notes": ""}, ok=(200, 302))
                if res.status_code == 200:  # pas assez de matières : achat d'urgence puis on refabrique
                    buy(day, {"Maïs grain": 2500, "Tourteau de soja": 1000, "Son de riz": 500, "Poudre de coquillage": 500,
                              "Prémix ponte": 50})
                    last_buy = day
                    br.post("/provenderie/fabrication/nouvelle", {"formula_id": str(fid), "quantity": str(qty), "date": d, "notes": ""})
        # provende du jour (le soigneur suit les propositions du logiciel, à quelques grammes près)
        if lots:
            br.post("/lots/alimentation", {
                "date": d, "lot_id": [str(l["id"]) for l in lots],
                "formula_id": [str(sugs[l["id"]]["formula_id"] or "") for l in lots],
                "quantity": [f"{sugs[l['id']]['kg'] * rng.uniform(0.98, 1.02):.1f}".replace(".", ",") for l in lots]})
        # mortalité
        for l in lots:
            if rng.random() < 0.07:
                n = rng.choice([1, 1, 1, 2])
                br.post(f"/lots/{l['id']}/effectif", {"kind": "mort", "quantity": str(n), "date": d,
                                                      "notes": rng.choice(["", "", "Maladie", "Chaleur", "Écrasement"])})
        if k == days - 40:  # un jour de forte mortalité (à expliquer)
            l = lots[0]
            br.post(f"/lots/{l['id']}/effectif", {"kind": "mort", "quantity": "9", "date": d, "notes": ""})
        # ramassage des œufs
        rows = {"date": d, "lot_id": [], "trays": [], "eggs": [], "broken": [], "notes": []}
        with app.app_context():
            for l in lots:
                rate = _laying_rate(lot_week(l, d), l["laying_week"] or 18, rng)
                note = ""
                if k in (days - 25, days - 24):  # coup de chaleur
                    rate *= 0.72
                    note = "Chaleur" if k == days - 25 else ""
                if rate <= 0:
                    continue
                hens = birds_on(l, d)
                laid = int(hens * rate)
                broken = int(laid * rng.uniform(0.005, 0.02))
                good = laid - broken
                rows["lot_id"].append(str(l["id"]))
                rows["trays"].append(str(good // 30))
                rows["eggs"].append(str(good % 30))
                rows["broken"].append(str(broken))
                rows["notes"].append(note)
        if rows["lot_id"]:
            br.post("/oeufs/", rows)
        # ventes (presque tous les jours, par plateaux)
        with app.app_context():
            stock = egg_stock(d)
        if stock >= 300 and rng.random() < 0.8:
            trays = int(stock * rng.uniform(0.5, 0.85)) // 30
            parts = [max(1, int(trays * p)) for p in (0.5, 0.3, 0.2)]
            for t in parts:
                if t <= 0:
                    continue
                r = rng.random()
                br.post("/ventes/nouvelle", {
                    "date": d, "client_id": "__new__:" + rng.choice(CLIENTS) if rng.random() < 0.8 else "", "product": "Œufs",
                    "trays": str(t), "eggs": "", "price": str(rng.choice([14000, 14500, 15000, 15000, 15500])), "price_unit": "plateau",
                    "paid": "0" if r < 0.12 else "", "account_id": str(caisse if rng.random() < 0.7 else mobile), "lot_id": "",
                    "notes": ""})
                if r < 0.12:
                    with app.app_context():
                        client_debt_sales.append((query("SELECT MAX(id) AS i FROM sales", one=True)["i"], day))
        # les clients paient leurs crédits 5 à 12 jours plus tard
        for sale_id, sold_day in list(client_debt_sales):
            if (day - sold_day).days >= rng.randint(5, 12) and day < today - timedelta(days=3):
                with app.app_context():
                    s = query("SELECT total FROM sales WHERE id = ?", (sale_id,), one=True)
                if s:
                    br.post(f"/ventes/{sale_id}/encaisser", {"amount": str(int(s["total"])), "date": d, "account_id": str(caisse)})
                client_debt_sales.remove((sale_id, sold_day))
        # fumier tous les mois
        if day.day == 15:
            br.post("/ventes/nouvelle", {"date": d, "client_id": "__new__:Coopérative maraîchère", "product": "Fumier",
                                         "qty": str(rng.randint(8, 15)), "unit": "sac", "price": "5000", "price_unit": "unite",
                                         "paid": "", "account_id": str(caisse), "lot_id": "", "notes": ""})
        # inventaires le 1er du mois : petits écarts, parfois expliqués
        if day.day == 1 and k > 5:
            with app.app_context():
                mats = query("SELECT id, name FROM materials WHERE active = 1")
                counts = {m["id"]: query("SELECT COALESCE(SUM(quantity), 0) AS q FROM stock_moves WHERE material_id = ?",
                                         (m["id"],), one=True)["q"] for m in mats}
            data = {"date": d}
            for m in mats:
                gap = rng.choice([0, 0, 0, -2, -5, -10]) if m["name"] != "Prémix ponte" else 0
                data[f"count_{m['id']}"] = f"{max(0, counts[m['id']] + gap):.1f}".replace(".", ",")
                data[f"why_{m['id']}"] = rng.choice(["Rats", "Humidité", "Erreur de pesée", ""]) if gap else ""
            br.post("/matieres/inventaire", data, ok=(200, 302))
            with app.app_context():
                st = egg_stock(d)
            if st > 60:
                lost = rng.choice([0, 6, 12, 30])
                br.post("/ventes/stock", {"kind": "inventaire", "trays": str((st - lost) // 30), "eggs": str((st - lost) % 30),
                                          "date": d, "notes": rng.choice(["Casse au stockage", ""]) if lost else ""})
        # cassés déclarés de temps en temps
        if rng.random() < 0.05:
            with app.app_context():
                st = egg_stock(d)
            if st > 30:
                br.post("/ventes/stock", {"kind": "casse", "trays": "0", "eggs": str(rng.randint(3, 12)), "date": d,
                                          "notes": "Casse au transport"})
    # quelques crédits encore en cours, une vente récente à crédit
    with app.app_context():
        st = egg_stock()
    if st > 300:
        br.post("/ventes/nouvelle", {"date": today.isoformat(), "client_id": "__new__:Hôtel Les Flamboyants", "product": "Œufs",
                                     "trays": "8", "eggs": "", "price": "15000", "price_unit": "plateau", "paid": "50000",
                                     "account_id": str(caisse), "lot_id": "", "notes": "Livré à l'hôtel, le reste à la fin du mois"})
    # messages d'exemple dans la Discussion
    br.post("/discussion/envoyer", {"body": "Bonjour Rasoa, la ponte du Lot A remonte bien après la chaleur 👍"}, ok=(200, 302))
    with app.app_context():
        return {"lots": query("SELECT COUNT(*) AS n FROM lots", one=True)["n"],
                "eggs": query("SELECT COALESCE(SUM(good), 0) AS n FROM egg_collections", one=True)["n"],
                "sales": query("SELECT COUNT(*) AS n FROM sales", one=True)["n"],
                "productions": query("SELECT COUNT(*) AS n FROM productions", one=True)["n"],
                "purchases": query("SELECT COUNT(*) AS n FROM purchases", one=True)["n"]}


# ---------------------------------------------------------------------------
# Remise à zéro quotidienne : on recharge la copie et on avance ses dates jusqu'à aujourd'hui
# ---------------------------------------------------------------------------
DATE_COLS = ("date", "arrival_date", "end_date")
STAMP_COLS = ("created_at", "last_login")


def shift_dates(conn, days):
    if not days:
        return
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for c in cols:
            if c in DATE_COLS:
                conn.execute(f"UPDATE {table} SET {c} = date({c}, ?) WHERE {c} != '' AND {c} IS NOT NULL", (f"{days:+d} days",))
            elif c in STAMP_COLS:
                conn.execute(f"UPDATE {table} SET {c} = strftime('%Y-%m-%dT%H:%M:%S', {c}, ?) || CASE WHEN {c} LIKE '%+00:00' THEN '+00:00' ELSE '' END "
                             f"WHERE {c} != '' AND {c} IS NOT NULL", (f"{days:+d} days",))
    conn.commit()


def reset_if_needed(app):
    """Appelé avant chaque page sur le site de démo : une fois par jour, on remet les données d'exemple."""
    from .db import get_setting

    data_dir = os.path.dirname(app.config["DATABASE"])
    snap = os.path.join(data_dir, SNAPSHOT)
    if not os.path.exists(snap):
        return
    today = Date.today().isoformat()
    if get_setting("demo_day") == today:
        return
    reset(app)


def reset(app):
    from .backups import restore_from
    from .db import get_db

    data_dir = os.path.dirname(app.config["DATABASE"])
    snap = os.path.join(data_dir, SNAPSHOT)
    restore_from(snap)
    conn = get_db()
    built = conn.execute("SELECT value FROM settings WHERE key = 'demo_built'").fetchone()
    built_day = Date.fromisoformat(built[0]) if built else Date.today()
    shift_dates(conn, (Date.today() - built_day).days)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('demo_day', ?)", (Date.today().isoformat(),))
    conn.commit()


def build_snapshot(app, days=180):
    """Construit la base de démo dans le dossier de données et en garde une copie (demo-snapshot.db)."""
    db_path = app.config["DATABASE"]
    data_dir = os.path.dirname(db_path)
    for f in os.listdir(data_dir):
        if f.startswith("ferme.db"):
            os.remove(os.path.join(data_dir, f))
    from .db import close_db, connect, ensure_super_admin, migrate, seed_defaults

    with app.app_context():
        close_db()
    conn = connect(db_path)
    migrate(conn)
    ensure_super_admin(conn)
    seed_defaults(conn)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_backup', '0')")
    conn.commit()
    conn.close()
    summary = build(app, days=days)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('demo_built', ?)", (Date.today().isoformat(),))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('demo_day', ?)", (Date.today().isoformat(),))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    shutil.copyfile(db_path, os.path.join(data_dir, SNAPSHOT))
    return summary
