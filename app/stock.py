"""Calculs de stock : quantités, prix moyen, autonomie, alertes.

Le stock n'est jamais « tapé » : il est toujours la somme des mouvements
(entrées positives, sorties négatives). Il ne peut donc pas devenir faux.
"""
from datetime import timedelta

from .db import query
from .utils import local_now

EPS = 1e-9


def parse_num(text, default=None):
    """Accepte « 12,5 », « 12.5 », « 1 250 » ; retourne None si vide ou invalide."""
    if text is None:
        return default
    cleaned = str(text).strip().replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    if cleaned == "":
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def today():
    return local_now().strftime("%Y-%m-%d")


def valid_date(text):
    from datetime import datetime

    try:
        datetime.strptime(text or "", "%Y-%m-%d")
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Prix moyen pondéré, recalculé en rejouant les mouvements
# ---------------------------------------------------------------------------
def replay_average(moves):
    stock, avg = 0.0, 0.0
    for move in moves:
        qty, cost = move["quantity"], move["unit_cost"]
        if qty > 0 and cost > 0:
            base = max(stock, 0.0)
            avg = (base * avg + qty * cost) / (base + qty)
        stock += qty
    return avg


def recompute_material(conn, material_id):
    moves = conn.execute(
        "SELECT quantity, unit_cost FROM stock_moves WHERE material_id = ? ORDER BY date, id", (material_id,)
    ).fetchall()
    conn.execute("UPDATE materials SET avg_cost = ? WHERE id = ?", (replay_average(moves), material_id))


def recompute_formula(conn, formula_id):
    moves = conn.execute(
        "SELECT quantity, unit_cost FROM feed_moves WHERE formula_id = ? ORDER BY date, id", (formula_id,)
    ).fetchall()
    conn.execute("UPDATE formulas SET avg_cost = ? WHERE id = ?", (replay_average(moves), formula_id))


# ---------------------------------------------------------------------------
# Niveaux de stock
# ---------------------------------------------------------------------------
def material_stock(material_id, conn=None):
    sql = "SELECT COALESCE(SUM(quantity), 0) AS q FROM stock_moves WHERE material_id = ?"
    row = (conn.execute(sql, (material_id,)).fetchone() if conn else query(sql, (material_id,), one=True))
    return row["q"] or 0.0


def feed_stock(formula_id, conn=None):
    sql = "SELECT COALESCE(SUM(quantity), 0) AS q FROM feed_moves WHERE formula_id = ?"
    row = (conn.execute(sql, (formula_id,)).fetchone() if conn else query(sql, (formula_id,), one=True))
    return row["q"] or 0.0


def _days_window(days=30):
    return (local_now() - timedelta(days=days)).strftime("%Y-%m-%d")


def materials_overview(include_inactive=False):
    """Toutes les matières avec stock, valeur, consommation et autonomie."""
    since = _days_window(30)
    rows = query(
        f"""SELECT m.*, c.name AS category,
                   COALESCE((SELECT SUM(quantity) FROM stock_moves s WHERE s.material_id = m.id), 0) AS stock,
                   COALESCE((SELECT -SUM(quantity) FROM stock_moves s WHERE s.material_id = m.id
                             AND s.quantity < 0 AND s.kind = 'fabrication' AND s.date >= ?), 0) AS used_30
            FROM materials m LEFT JOIN categories c ON c.id = m.category_id
            {'' if include_inactive else 'WHERE m.active = 1'}
            ORDER BY c.name IS NULL, c.name, m.name""",
        (since,),
    )
    result = []
    for row in rows:
        item = dict(row)
        item["value"] = max(item["stock"], 0) * item["avg_cost"]
        daily = item["used_30"] / 30 if item["used_30"] > 0 else 0
        item["daily_use"] = daily
        item["autonomy_days"] = (item["stock"] / daily) if daily > 0 else None
        item["status"] = stock_status(item["stock"], item["alert_threshold"])
        result.append(item)
    return result


def formulas_overview(include_inactive=False):
    since = _days_window(30)
    rows = query(
        f"""SELECT f.*,
                   COALESCE((SELECT SUM(quantity) FROM feed_moves s WHERE s.formula_id = f.id), 0) AS stock,
                   COALESCE((SELECT -SUM(quantity) FROM feed_moves s WHERE s.formula_id = f.id
                             AND s.quantity < 0 AND s.kind = 'distribution' AND s.date >= ?), 0) AS used_30,
                   (SELECT COUNT(*) FROM formula_lines l WHERE l.formula_id = f.id) AS nb_lines
            FROM formulas f {'' if include_inactive else 'WHERE f.active = 1'}
            ORDER BY f.name""",
        (since,),
    )
    result = []
    for row in rows:
        item = dict(row)
        item["value"] = max(item["stock"], 0) * item["avg_cost"]
        daily = item["used_30"] / 30 if item["used_30"] > 0 else 0
        item["autonomy_days"] = (item["stock"] / daily) if daily > 0 else None
        item["status"] = stock_status(item["stock"], item["alert_threshold"])
        result.append(item)
    return result


def stock_status(stock, threshold):
    if stock <= EPS:
        return "empty"
    if threshold and stock <= threshold:
        return "low"
    return "ok"


def formula_lines(formula_id, conn=None):
    sql = """SELECT l.*, m.name, m.unit, m.avg_cost,
                    COALESCE((SELECT SUM(quantity) FROM stock_moves s WHERE s.material_id = m.id), 0) AS stock
             FROM formula_lines l JOIN materials m ON m.id = l.material_id
             WHERE l.formula_id = ? ORDER BY l.quantity DESC, m.name"""
    return conn.execute(sql, (formula_id,)).fetchall() if conn else query(sql, (formula_id,))


def production_needs(formula, quantity, conn=None):
    """Besoins en matières pour fabriquer `quantity` kg de provende."""
    lines = formula_lines(formula["id"], conn)
    base = formula["base_qty"] or 100
    needs = []
    for line in lines:
        need = line["quantity"] * quantity / base
        needs.append({
            "material_id": line["material_id"], "name": line["name"], "unit": line["unit"],
            "need": need, "stock": line["stock"], "unit_cost": line["avg_cost"],
            "cost": need * line["avg_cost"], "missing": max(0.0, need - line["stock"]),
        })
    return needs


def formula_cost_per_kg(formula):
    lines = formula_lines(formula["id"])
    base = formula["base_qty"] or 100
    from .units import to_kg

    total_qty = sum((to_kg(line["quantity"], line["unit"]) if to_kg(line["quantity"], line["unit"]) is not None
                     else line["quantity"]) for line in lines)
    cost = sum(line["quantity"] * line["avg_cost"] for line in lines)
    return (cost / base) if base else 0, total_qty


# ---------------------------------------------------------------------------
# Comptes (caisses et propriétaires) et dettes fournisseurs
# ---------------------------------------------------------------------------
def accounts(active_only=True):
    return query(f"SELECT * FROM accounts {'WHERE active = 1' if active_only else ''} ORDER BY sort, name")


def supplier_balance(supplier_id):
    bought = query("SELECT COALESCE(SUM(total), 0) AS t, COALESCE(SUM(paid), 0) AS p FROM purchases WHERE supplier_id = ?",
                   (supplier_id,), one=True)
    paid_later = query("SELECT COALESCE(SUM(amount), 0) AS a FROM supplier_payments WHERE supplier_id = ?",
                       (supplier_id,), one=True)["a"]
    return {"bought": bought["t"], "paid": bought["p"] + paid_later, "due": bought["t"] - bought["p"] - paid_later}


def inventory_gaps(since=None, material_id=None, limit=200):
    """Écarts d'inventaire : pour chaque comptage, le stock du logiciel avant,
    la quantité comptée, l'écart et sa valeur. Calculé depuis l'historique,
    donc les anciens inventaires sont aussi visibles."""
    where = ["s.kind = 'inventaire'"]
    params = []
    if since:
        where.append("s.date >= ?")
        params.append(since)
    if material_id:
        where.append("s.material_id = ?")
        params.append(material_id)
    rows = query(
        f"""SELECT s.*, m.name, m.unit, u.username, u.full_name,
                   COALESCE((SELECT SUM(t.quantity) FROM stock_moves t
                             WHERE t.material_id = s.material_id AND t.id < s.id), 0) AS before
            FROM stock_moves s JOIN materials m ON m.id = s.material_id
            LEFT JOIN users u ON u.id = s.created_by
            WHERE {' AND '.join(where)} ORDER BY s.date DESC, s.id DESC LIMIT ?""",
        (*params, limit),
    )
    result = []
    for row in rows:
        item = dict(row)
        item["after"] = item["before"] + item["quantity"]
        item["value"] = item["quantity"] * (item["unit_cost"] or 0)
        result.append(item)
    return result


def last_moves():
    """Dernier mouvement de chaque matière : avant → après et la raison
    (achat, fabrication de telle provende, perte, inventaire…)."""
    rows = query(
        """SELECT s.*, f.name AS formula, p.id AS production_id,
                  COALESCE((SELECT SUM(t.quantity) FROM stock_moves t
                            WHERE t.material_id = s.material_id AND t.id < s.id), 0) AS before
           FROM stock_moves s
           LEFT JOIN productions p ON s.ref_type = 'production' AND p.id = s.ref_id
           LEFT JOIN formulas f ON f.id = p.formula_id
           WHERE s.id = (SELECT x.id FROM stock_moves x WHERE x.material_id = s.material_id
                         ORDER BY x.date DESC, x.id DESC LIMIT 1)""")
    result = {}
    for row in rows:
        item = dict(row)
        item["after"] = item["before"] + item["quantity"]
        result[item["material_id"]] = item
    return result
