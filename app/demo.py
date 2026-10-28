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


class _ctx:
    """Contexte de l'application qui travaille sur la base de démo."""

    def __init__(self, app):
        self.c = app.app_context()

    def __enter__(self):
        from flask import g

        self.c.__enter__()
        g.demo = True
        return self

    def __exit__(self, *exc):
        return self.c.__exit__(*exc)


class _Client:
    """Navigateur simulé : passe par les vraies pages du logiciel."""

    def __init__(self, app):
        self.app = app
        self.c = app.test_client()
        with self.c.session_transaction() as s:
            s["_csrf"] = "demo"
            s["demo"] = True

    def post(self, url, data, ok=(302,)):
        with self.c.session_transaction() as s:  # la connexion renouvelle la session
            s.setdefault("_csrf", "demo")
            s["demo"] = True
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
    with _ctx(app):
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

    with _ctx(app):
        caisse = query("SELECT id FROM accounts WHERE name = 'Caisse de la ferme'", one=True)["id"]
        proprio = query("SELECT id FROM accounts WHERE name LIKE 'Rakoto%'", one=True)["id"]
        mobile = query("SELECT id FROM accounts WHERE name = 'Mobile money'", one=True)["id"]

    # --- matières, formules, programme ---------------------------------------
    mat_ids, prices, suppliers = {}, {}, {}
    for name, cat, unit, threshold, price, supplier in MATERIALS:
        br.post("/matieres/matiere/nouvelle", {"name": name, "category_name": cat, "unit": unit,
                                               "alert_threshold": str(threshold), "notes": ""})
        with _ctx(app):
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

    with _ctx(app):
        associee = query("SELECT id FROM accounts WHERE name LIKE 'Rasoa%'", one=True)["id"]
    # argent de départ apporté par les deux propriétaires (Caisse & propriétaires → Apport)
    d0 = (start - timedelta(days=1)).isoformat()
    br.post("/caisse/operation", {"op": "apport", "date": d0, "owner_id": str(proprio), "account_id": str(caisse),
                                  "amount": "20000000", "notes": "Apport de départ"})
    br.post("/caisse/operation", {"op": "apport", "date": d0, "owner_id": str(associee), "account_id": str(caisse),
                                  "amount": "14000000", "notes": "Apport de départ"})
    br.post("/caisse/parts", {f"share_{proprio}": "60", f"share_{associee}": "40"})
    # immobilisations : bâtiments et cages déjà là avant le logiciel, groupe électrogène acheté pendant la période
    for name, cat, ago, cost, years, acc in [("Bâtiments 1 et 2", "Bâtiment", 420, 18000000, 20, ""),
                                             ("Bâtiment 3", "Bâtiment", 200, 7000000, 20, ""),
                                             ("Cages et pondoirs", "Cages / batteries", 300, 6000000, 10, ""),
                                             ("Abreuvoirs et mangeoires", "Abreuvoirs / mangeoires", 240, 900000, 5, ""),
                                             ("Groupe électrogène 5 kVA", "Groupe électrogène", 150, 3500000, 5, str(caisse))]:
        br.post("/immobilisations/nouveau", {"name": name, "category": cat, "purchase_date": (today - timedelta(days=ago)).isoformat(),
                                             "cost": str(cost), "life_years": str(years), "residual": "", "lot_id": "",
                                             "account_id": acc if today - timedelta(days=ago) >= start else "", "notes": ""})
    # personnel
    for name, job, pay_type, salary in [("Hery", "Soigneur", "mois", 250000), ("Fara", "Ramasseuse d'œufs", "mois", 180000),
                                        ("Koto", "Gardien de nuit", "jour", 8000)]:
        br.post("/personnel/employe/nouveau", {"name": name, "job": job, "phone": "", "pay_type": pay_type, "salary": str(salary),
                                               "start_date": (start - timedelta(days=30)).isoformat(), "notes": ""})
    with _ctx(app):
        staff_ids = [r["id"] for r in query("SELECT id FROM employees ORDER BY id")]
    buy(start, {"Maïs grain": 3000, "Tourteau de soja": 1200, "Son de riz": 600, "Poudre de coquillage": 500, "Prémix ponte": 60})

    formula_ids = {}
    for fname, recipe in FORMULAS.items():
        br.post("/provenderie/formules/nouvelle", {
            "name": fname, "base_qty": "100", "notes": "",
            "material_id": [str(mat_ids[m]) for m in recipe], "quantity": [str(q) for q in recipe.values()],
            "qty_unit": ["kg"] * len(recipe)})
        with _ctx(app):
            formula_ids[fname] = query("SELECT id FROM formulas WHERE name = ?", (fname,), one=True)["id"]
    br.post("/provenderie/programmes/nouveau", {
        "name": "Programme pondeuses", "notes": "Exemple : une provende par période, les grammes augmentent avec l'âge.",
        "week_from": [str(a) for a, _, _, _ in PROGRAM], "week_to": [str(b) for _, b, _, _ in PROGRAM],
        "formula_id": [str(formula_ids[f]) for _, _, f, _ in PROGRAM], "grams_per_bird": [str(g) for *_, g in PROGRAM]})
    with _ctx(app):
        prog = query("SELECT id FROM feed_programs", one=True)["id"]

    # --- programme sanitaire (exemple) ---------------------------------------
    from .health import EXAMPLE

    br.post("/sanitaire/programmes/nouveau", {
        "name": "Programme pondeuses (exemple)", "notes": "Exemple à adapter avec votre vétérinaire.",
        "day_age": [str(d) for d, *_ in EXAMPLE], "title": [t for _, t, _, _ in EXAMPLE], "kind": [k for _, _, k, _ in EXAMPLE],
        "method": [m for *_, m in EXAMPLE], "product": ["" for _ in EXAMPLE]})
    with _ctx(app):
        health_prog = query("SELECT id FROM health_programs", one=True)["id"]
    PRODUCTS = {"vaccin": ("Vaccin (flacon 1000 doses)", 9000, 25000), "vitamines": ("Vitamines AD3E", 6000, 12000),
                "vermifuge": ("Pipérazine", 8000, 15000)}

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
            "health_program_id": str(health_prog),
            "egg_price": "500", "supplier": "Couvoir Rabe", "chick_price": "3500", "other_costs": "60000",
            "paid": "", "account_id": str(proprio), "notes": ""})
        with _ctx(app):
            created[name] = query("SELECT * FROM lots WHERE name = ?", (name,), one=True)

    for name, ago, count, building, laying in lots_plan:
        if today - timedelta(days=ago) <= start:
            create_lot(name, today - timedelta(days=ago), count, building, laying)

    # --- jour après jour -----------------------------------------------------
    from .eggs import birds_on
    from .lots import lot_week, suggestion
    from .sales import egg_stock
    from .stock import feed_stock
    from . import manure as manure_batches_mod

    client_debt_sales = []
    last_buy = start
    rng2 = random.Random(seed + 101)  # prix des œufs et fumier (n'influence pas le reste de l'exemple)
    egg_price = 14500
    manure_last = {}
    for k in range(days):
        day = start + timedelta(days=k)
        d = day.isoformat()
        for name, ago, count, building, laying in lots_plan:
            if name not in created and today - timedelta(days=ago) == day:
                create_lot(name, day, count, building, laying)
        with _ctx(app):
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
        with _ctx(app):
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
        # prix du plateau : il change de temps en temps (marché, saison)
        if k == 0 or rng2.random() < 0.06:
            step = rng2.choice([-1000, -500, -500, 500, 500, 1000]) if k else 0
            step = -abs(step) if egg_price >= 16000 else (abs(step) if egg_price <= 14000 else step)
            egg_price = max(13000, min(17000, egg_price + step))
            br.post("/prix-oeufs/changer", {"date": d, "tray_price": str(egg_price), "unit": "plateau",
                                            "notes": "" if k == 0 else rng2.choice(["", "Marché", "Fêtes", "Beaucoup d'œufs au marché",
                                                                                    "Saison des pluies"])})
        # fumier : enlevé tous les ~3 semaines par lot, vendu quand il est sec (14 jours)
        for l in lots:
            if (day - manure_last.get(l["id"], Date.fromisoformat(l["arrival_date"]))).days >= rng2.randint(20, 26):
                br.post("/fumier/enlever", {"date": d, "lot_id": str(l["id"]), "quantity": str(max(4, l["initial_count"] // 20 + rng2.randint(-3, 3))),
                                            "unit": "sac", "dry_days": "14", "notes": ""})
                manure_last[l["id"]] = day
        if rng2.random() < 0.25 and day < today - timedelta(days=2):
            with _ctx(app):
                ready = [b for b in manure_batches_mod.batches(include_empty=False) if b["ready_date"] <= d and b["left"] > 0]
            for b in ready[:1]:
                q = max(1, int(b["left"] * rng2.uniform(0.5, 1)))
                br.post("/fumier/vendre", {"batch_id": str(b["id"]), "date": d, "quantity": str(q), "price": str(rng2.choice([2500, 3000, 3000, 3500])),
                                           "price_mode": "unite", "client_id": "__new__:" + rng2.choice(["Maraîcher Ivato", "Jardin Tsiazotafo", "Coopérative riz"]),
                                           "paid": "", "account_id": str(caisse)})
        # ventes (presque tous les jours, par plateaux)
        with _ctx(app):
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
                    "trays": str(t), "eggs": "", "price": str(egg_price + (rng.choice([14000, 14500, 15000, 15000, 15500]) - 15000) // 2),
                    "price_unit": "plateau",
                    "paid": "0" if r < 0.12 else "", "account_id": str(caisse if rng.random() < 0.7 else mobile), "lot_id": "",
                    "notes": ""})
                if r < 0.12:
                    with _ctx(app):
                        client_debt_sales.append((query("SELECT MAX(id) AS i FROM sales", one=True)["i"], day))
        # les clients paient leurs crédits 5 à 12 jours plus tard
        for sale_id, sold_day in list(client_debt_sales):
            if (day - sold_day).days >= rng.randint(5, 12) and day < today - timedelta(days=3):
                with _ctx(app):
                    s = query("SELECT total FROM sales WHERE id = ?", (sale_id,), one=True)
                if s:
                    br.post(f"/ventes/{sale_id}/encaisser", {"amount": str(int(s["total"])), "date": d, "account_id": str(caisse)})
                client_debt_sales.remove((sale_id, sold_day))
        # soins faits le jour prévu (ou 1 à 2 jours après) ; les tout derniers restent à confirmer pour la démo
        if day < today - timedelta(days=1):
            with _ctx(app):
                due = query("SELECT * FROM health_events WHERE status = 'prevu' AND due_date <= ? AND due_date < ?",
                            (d, (today - timedelta(days=5)).isoformat()))  # les soins des 5 derniers jours restent à confirmer
            for ev in due:
                if rng.random() < 0.6 and ev["due_date"] == d:
                    continue  # fait un peu plus tard
                prod, lo, hi = PRODUCTS.get(ev["kind"], ("", 0, 0))
                br.post(f"/sanitaire/soin/{ev['id']}/fait", {
                    "done_date": d, "product": ev["product"] or prod, "dose": "", "cost": str(rng.randint(lo, hi) // 500 * 500) if hi else "",
                    "account_id": str(caisse), "notes": ""})
        # dépenses de la ferme
        if day.day == 6:
            br.post("/depenses/nouvelle", {"date": d, "category": "Électricité", "label": "Facture JIRAMA", "amount": str(rng.randint(120, 190) * 1000),
                                           "lot_id": "", "account_id": str(caisse), "beneficiary": "JIRAMA", "notes": ""})
            br.post("/depenses/nouvelle", {"date": d, "category": "Eau", "label": "", "amount": str(rng.randint(30, 50) * 1000),
                                           "lot_id": "", "account_id": str(mobile), "beneficiary": "", "notes": ""})
        if day.day in (3, 17):
            br.post("/depenses/nouvelle", {"date": d, "category": "Transport", "label": "Livraison des œufs",
                                           "amount": str(rng.randint(35, 80) * 1000), "lot_id": "", "account_id": str(caisse),
                                           "beneficiary": "Taxi-brousse", "notes": ""})
        if day.day == 12:
            with _ctx(app):
                some = query("SELECT id FROM lots WHERE status = 'actif' AND arrival_date <= ? ORDER BY RANDOM() LIMIT 1", (d,), one=True)
            if some:
                br.post("/depenses/nouvelle", {"date": d, "category": "Litière / copeaux", "label": "", "amount": str(rng.randint(20, 45) * 1000),
                                               "lot_id": str(some["id"]), "account_id": str(caisse), "beneficiary": "Scierie", "notes": ""})
        if day.day == 22 and rng.random() < 0.5:
            br.post("/depenses/nouvelle", {"date": d, "category": "Réparation / entretien", "label": rng.choice(["Toiture", "Porte du bâtiment", "Pompe"]),
                                           "amount": str(rng.randint(50, 150) * 1000), "lot_id": "", "account_id": str(caisse),
                                           "beneficiary": "", "notes": ""})
        if k == days - 9:  # une réparation pas encore payée (dette)
            br.post("/depenses/nouvelle", {"date": d, "category": "Réparation / entretien", "label": "Grillage du bâtiment 2",
                                           "amount": "85000", "lot_id": "", "account_id": str(caisse), "beneficiary": "Soudeur Naina",
                                           "notes": "À payer à la fin du mois", "unpaid": "1"})
        # salaires payés le dernier jour du mois (une avance le 15 pour le soigneur)
        nxt = day + timedelta(days=1)
        if day.day == 15:
            br.post("/personnel/paiement", {"employee_id": str(staff_ids[0]), "kind": "avance", "month": d[:7], "date": d,
                                            "amount": "50000", "account_id": str(caisse), "notes": ""})
        if nxt.month != day.month:
            from .staff import month_sheet

            for eid in staff_ids:
                with _ctx(app):
                    s = month_sheet(query("SELECT * FROM employees WHERE id = ?", (eid,), one=True), d[:7])
                if s["rest"] > 0.5:
                    br.post("/personnel/paiement", {"employee_id": str(eid), "kind": "salaire", "month": d[:7], "date": d,
                                                    "amount": str(int(round(s["rest"]))), "account_id": str(caisse), "notes": ""})
            # le mobile money revient dans la caisse
            with _ctx(app):
                mm = query("SELECT COALESCE(SUM(amount), 0) AS b FROM cash_movements WHERE account_id = ?", (mobile,), one=True)["b"]
            if mm > 200000:
                br.post("/caisse/operation", {"op": "transfert", "date": d, "from_id": str(mobile), "account_id": str(caisse),
                                              "amount": str(int(mm - 100000)), "notes": "Retrait mobile money"})
        if k == days - 50:  # comptage de la caisse : il manque un peu d'argent (expliqué)
            with _ctx(app):
                bal = query("SELECT COALESCE(SUM(amount), 0) AS b FROM cash_movements WHERE account_id = ? AND date <= ?", (caisse, d), one=True)["b"]
            br.post("/caisse/operation", {"op": "comptage", "date": d, "account_id": str(caisse), "counted": str(int(bal - 15000)),
                                          "notes": "Monnaie rendue en trop"})
        if k == days - 20:
            br.post("/caisse/operation", {"op": "retrait", "date": d, "owner_id": str(proprio), "account_id": str(caisse),
                                          "amount": "500000", "notes": "Besoin personnel"})
        if k == days - 30 and "Lot A" in created:  # quelques poules faibles vendues en réforme
            br.post("/reformes/vendre", {"lot_id": str(created["Lot A"]["id"]), "date": d, "birds": "25", "price_mode": "poule",
                                         "price": "15000", "client_id": "__new__:Boucherie Rado", "paid": "", "account_id": str(caisse),
                                         "notes": "Poules faibles, ne pondaient plus"})
        # fumier tous les mois
        if day.day == 15:
            br.post("/ventes/nouvelle", {"date": d, "client_id": "__new__:Coopérative maraîchère", "product": "Fumier",
                                         "qty": str(rng.randint(8, 15)), "unit": "sac", "price": "5000", "price_unit": "unite",
                                         "paid": "", "account_id": str(caisse), "lot_id": "", "notes": ""})
        # inventaires le 1er du mois : petits écarts, parfois expliqués
        if day.day == 1 and k > 5:
            with _ctx(app):
                mats = query("SELECT id, name FROM materials WHERE active = 1")
                counts = {m["id"]: query("SELECT COALESCE(SUM(quantity), 0) AS q FROM stock_moves WHERE material_id = ?",
                                         (m["id"],), one=True)["q"] for m in mats}
            data = {"date": d}
            for m in mats:
                gap = rng.choice([0, 0, 0, -2, -5, -10]) if m["name"] != "Prémix ponte" else 0
                data[f"count_{m['id']}"] = f"{max(0, counts[m['id']] + gap):.1f}".replace(".", ",")
                data[f"why_{m['id']}"] = rng.choice(["Rats", "Humidité", "Erreur de pesée", ""]) if gap else ""
            br.post("/matieres/inventaire", data, ok=(200, 302))
            with _ctx(app):
                st = egg_stock(d)
            if st > 60:
                lost = rng.choice([0, 6, 12, 30])
                br.post("/ventes/stock", {"kind": "inventaire", "trays": str((st - lost) // 30), "eggs": str((st - lost) % 30),
                                          "date": d, "notes": rng.choice(["Casse au stockage", ""]) if lost else ""})
        # cassés déclarés de temps en temps
        if rng.random() < 0.05:
            with _ctx(app):
                st = egg_stock(d)
            if st > 30:
                br.post("/ventes/stock", {"kind": "casse", "trays": "0", "eggs": str(rng.randint(3, 12)), "date": d,
                                          "notes": "Casse au transport"})
    # quelques crédits encore en cours, une vente récente à crédit
    with _ctx(app):
        st = egg_stock()
    if st > 300:
        br.post("/ventes/nouvelle", {"date": today.isoformat(), "client_id": "__new__:Hôtel Les Flamboyants", "product": "Œufs",
                                     "trays": "8", "eggs": "", "price": "15000", "price_unit": "plateau", "paid": "50000",
                                     "account_id": str(caisse), "lot_id": "", "notes": "Livré à l'hôtel, le reste à la fin du mois"})
    # messages d'exemple dans la Discussion
    br.post("/discussion/envoyer", {"body": "Bonjour Rasoa, la ponte du Lot A remonte bien après la chaleur 👍"}, ok=(200, 302))
    with _ctx(app):  # les visiteurs ne voient pas 60 messages « non lus » en arrivant
        conn = get_db()
        conn.execute("UPDATE users SET last_seen_message = COALESCE((SELECT MAX(id) FROM messages), 0) - 3")
        conn.commit()
    # un soin en retard (jamais confirmé) pour montrer l'alerte rouge permanente
    with _ctx(app):
        ev = query("""SELECT e.id FROM health_events e JOIN lots l ON l.id = e.lot_id
                      WHERE l.name = 'Lot D' AND e.status = 'fait' ORDER BY e.due_date DESC LIMIT 1""", one=True)
    if ev:
        br.post(f"/sanitaire/soin/{ev['id']}/modifier", {"action": "rouvrir"})
    with _ctx(app):
        return {"lots": query("SELECT COUNT(*) AS n FROM lots", one=True)["n"],
                "eggs": query("SELECT COALESCE(SUM(good), 0) AS n FROM egg_collections", one=True)["n"],
                "sales": query("SELECT COUNT(*) AS n FROM sales", one=True)["n"],
                "productions": query("SELECT COUNT(*) AS n FROM productions", one=True)["n"],
                "purchases": query("SELECT COUNT(*) AS n FROM purchases", one=True)["n"]}


# ---------------------------------------------------------------------------
# Remise à zéro quotidienne : on recharge la copie et on avance ses dates jusqu'à aujourd'hui
# ---------------------------------------------------------------------------
DATE_COLS = ("date", "arrival_date", "end_date", "due_date", "done_date", "paid_date", "purchase_date", "start_date")
STAMP_COLS = ("created_at", "last_login")


def shift_dates(conn, days):
    if not days:
        return
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for c in cols:
            if c in DATE_COLS:
                conn.execute(f"UPDATE {table} SET {c} = date({c}, ?) WHERE {c} != '' AND {c} IS NOT NULL", (f"{days:+d} days",))
            elif c == "month" and table == "staff_payments":  # mois AAAA-MM : on décale le milieu du mois
                conn.execute(f"UPDATE {table} SET month = strftime('%Y-%m', date(month || '-15', ?))", (f"{days:+d} days",))
            elif c in STAMP_COLS:
                conn.execute(f"UPDATE {table} SET {c} = datetime({c}, ?) WHERE {c} != '' AND {c} IS NOT NULL", (f"{days:+d} days",))
    conn.commit()


def demo_dir(app):
    return os.path.dirname(app.config["DEMO_DATABASE"])


def available(app):
    return os.path.exists(os.path.join(demo_dir(app), SNAPSHOT))


def reset_if_needed(app):
    """Appelé avant chaque page d'un visiteur de la démo : une fois par jour, on remet les données d'exemple."""
    path = app.config["DEMO_DATABASE"]
    stamp = os.path.join(demo_dir(app), "demo-day.txt")
    today = Date.today().isoformat()
    try:
        with open(stamp, encoding="utf-8") as fh:
            if fh.read().strip() == today and os.path.exists(path):
                return
    except OSError:
        pass
    reset(app)
    with open(stamp, "w", encoding="utf-8") as fh:
        fh.write(today)


def reset(app):
    """Recharge la copie de démo et avance ses dates pour que « aujourd'hui » soit vraiment aujourd'hui."""
    from .db import close_db

    close_db()
    snap = os.path.join(demo_dir(app), SNAPSHOT)
    tmp = app.config["DEMO_DATABASE"] + ".tmp"
    shutil.copyfile(snap, tmp)
    conn = sqlite3.connect(tmp)
    built = conn.execute("SELECT value FROM settings WHERE key = 'demo_built'").fetchone()
    built_day = Date.fromisoformat(built[0]) if built else Date.today()
    shift_dates(conn, (Date.today() - built_day).days)
    conn.close()
    from .db import connect, migrate

    conn = connect(tmp)  # une démo construite avec une ancienne version reçoit les nouvelles tables
    migrate(conn)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(app.config["DEMO_DATABASE"] + suffix)
        except OSError:
            pass
    os.replace(tmp, app.config["DEMO_DATABASE"])


def build_snapshot(app, days=180):
    """Construit la base de démo (dossier demo/) et en garde une copie (demo-snapshot.db)."""
    from .db import connect, ensure_super_admin, migrate, seed_defaults

    path = app.config["DEMO_DATABASE"]
    os.makedirs(demo_dir(app), exist_ok=True)
    for f in os.listdir(demo_dir(app)):
        if f.startswith("ferme.db") or f == "demo-day.txt":
            os.remove(os.path.join(demo_dir(app), f))
    conn = connect(path)
    migrate(conn)
    ensure_super_admin(conn)
    seed_defaults(conn)
    conn.commit()
    conn.close()
    app.config["DEMO_BUILDING"] = True
    try:
        summary = build(app, days=days)
    finally:
        app.config["DEMO_BUILDING"] = False
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('demo_built', ?)", (Date.today().isoformat(),))
    conn.commit()
    snap_path = os.path.join(demo_dir(app), SNAPSHOT)
    if os.path.exists(snap_path):
        os.remove(snap_path)
    snap = sqlite3.connect(snap_path)
    conn.backup(snap)  # copie complète et cohérente (y compris ce qui est encore dans le journal)
    conn.close()
    snap.execute("PRAGMA journal_mode = DELETE")
    snap.execute("VACUUM")
    snap.close()
    with open(os.path.join(demo_dir(app), "demo-day.txt"), "w", encoding="utf-8") as fh:
        fh.write(Date.today().isoformat())
    return summary


# ---------------------------------------------------------------------------
# Une copie personnelle pour chaque visiteur, effacée quand il se déconnecte
# ---------------------------------------------------------------------------
VISITOR_MAX_AGE = 3 * 3600   # une copie inutilisée depuis 3 heures est effacée
VISITOR_MAX = 60             # au-delà, les plus anciennes sont effacées


def visitors_dir(app):
    return os.path.join(demo_dir(app), "visiteurs")


def visitor_path():
    from flask import current_app, has_request_context, session

    if not has_request_context() or current_app.config.get("DEMO_BUILDING"):
        return None
    vid = session.get("demo_id")
    if not vid or not vid.isalnum():
        return None
    return os.path.join(visitors_dir(current_app), vid + ".db")


def _remove_db(path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def cleanup_visitors(app):
    import time

    folder = visitors_dir(app)
    if not os.path.isdir(folder):
        return
    files = []
    for f in os.listdir(folder):
        if f.endswith(".db"):
            path = os.path.join(folder, f)
            try:
                files.append((os.path.getmtime(path), path))
            except OSError:
                pass
    files.sort()
    now = time.time()
    for i, (mtime, path) in enumerate(files):
        if now - mtime > VISITOR_MAX_AGE or i < len(files) - VISITOR_MAX:
            _remove_db(path)


def new_visitor(app):
    """Crée la copie personnelle d'un visiteur à partir de la ferme d'exemple du jour."""
    import secrets

    from flask import session

    from .db import close_db

    end_visitor(app)
    cleanup_visitors(app)
    reset_if_needed(app)
    os.makedirs(visitors_dir(app), exist_ok=True)
    vid = secrets.token_hex(10)
    path = os.path.join(visitors_dir(app), vid + ".db")
    close_db()
    src = sqlite3.connect(app.config["DEMO_DATABASE"], timeout=30)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    session["demo_id"] = vid
    return path


def end_visitor(app):
    """Efface la copie du visiteur : tout ce qu'il a essayé disparaît."""
    from .db import close_db

    path = visitor_path()
    if path:
        close_db()
        _remove_db(path)


# ---------------------------------------------------------------------------
# Pages : entrer dans la démo, en sortir
# ---------------------------------------------------------------------------
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for  # noqa: E402

bp = Blueprint("demo", __name__, url_prefix="/demo")

# Ce qu'un visiteur ne peut pas faire dans la démo
BLOCKED = {"backups", "settings"}  # les comptes s'essaient : chaque visiteur a sa propre copie
BLOCKED_ENDPOINTS = {"auth.profile", "auth.first_setup", "auth.forgot", "auth.forgot_email", "auth.forgot_question"}


def guard():
    """Appelé avant chaque page : choisit la base de démo et bloque les actions sensibles."""
    g.demo = bool(session.get("demo"))
    if not g.demo or current_app.config.get("DEMO_BUILDING"):
        return None
    if not available(current_app._get_current_object()):
        session.clear()
        g.demo = False
        flash("La démonstration n'est pas disponible pour le moment.", "info")
        return redirect(url_for("auth.login"))
    app = current_app._get_current_object()
    path = visitor_path()
    if request.endpoint not in ("demo.index", "demo.enter", "demo.leave", "static") and (not path or not os.path.exists(path)):
        session.clear()
        g.demo = False
        flash("Votre essai de la démonstration est terminé (les données d'essai ont été effacées). "
              "Choisissez un rôle pour recommencer.", "info")
        return redirect(url_for("demo.index"))
    if path and os.path.exists(path):
        try:
            os.utime(path, None)  # le visiteur est toujours là : on garde sa copie
        except OSError:
            pass
    if request.method == "POST" and (request.blueprint in BLOCKED or request.endpoint in BLOCKED_ENDPOINTS):
        flash("Dans la démonstration, cette action est désactivée (sauvegardes, paramètres, mot de passe). "
              "Tout le reste peut être essayé librement.", "info")
        return redirect(request.referrer or url_for("main.dashboard"))
    if request.blueprint == "backups" and request.endpoint != "backups.index":
        flash("Dans la démonstration, les sauvegardes sont désactivées.", "info")
        return redirect(url_for("main.dashboard"))
    return None


@bp.route("/")
def index():
    if session.get("demo"):  # « Changer de rôle » : l'essai précédent est effacé
        end_visitor(current_app._get_current_object())
        session.clear()
        g.demo = False
    return render_template("demo/index.html", users=DEMO_USERS, ready=available(current_app._get_current_object()))


@bp.route("/entrer", methods=["POST"])
def enter():
    from datetime import datetime, timezone

    from .db import query

    app = current_app._get_current_object()
    if not available(app):
        flash("La démonstration n'est pas encore installée.", "error")
        return redirect(url_for("demo.index"))
    username = request.form.get("username", "")
    if username not in {u[0] for u in DEMO_USERS}:
        return redirect(url_for("demo.index"))
    end_visitor(app)
    session.clear()
    session["demo"] = True
    g.demo = True
    new_visitor(app)
    user = query("SELECT * FROM users WHERE username = ? AND deleted = 0", (username,), one=True)
    if user is None:
        session.clear()
        flash("Compte de démonstration introuvable.", "error")
        return redirect(url_for("demo.index"))
    session.permanent = True
    session["user_id"] = user["id"]
    session["last_seen"] = datetime.now(timezone.utc).timestamp()
    flash(f"Bienvenue dans la démonstration ! Vous êtes connecté comme « {user['full_name']} ». "
          "Essayez tout librement : tout ce que vous faites sera effacé quand vous vous déconnecterez.", "success")
    return redirect(url_for("main.dashboard"))


@bp.route("/sortir", methods=["POST", "GET"])
def leave():
    if session.get("demo"):
        end_visitor(current_app._get_current_object())
    session.clear()
    return redirect(url_for("demo.index"))
