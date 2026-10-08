"""Écarts & contrôles : tout ce qui manque ou ne colle pas, mis en évidence au même endroit.

Matières premières et provende : écarts d'inventaire et pertes déclarées.
Poules : disparitions, écarts de comptage, et jours de mortalité anormale.
Œufs : viendront avec le module Œufs.
Chaque écart doit avoir une explication ; ceux qui n'en ont pas sont signalés en rouge.
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, query
from .discussion import notify
from .security import EDIT, VIEW, can, login_required, require
from .stock import moves_with_balance, today, valid_date
from .utils import date_fr, fmt_money, fmt_qty, log_activity

bp = Blueprint("controles", __name__, url_prefix="/controles")

UNEXPLAINED = {"", "inventaire physique"}
DOMAINS = {
    "matiere": ("📦", "Matières premières"),
    "provende": ("🌾", "Provende"),
    "poules": ("🐔", "Poules"),
    "oeufs": ("🥚", "Œufs"),
}


def _explained(text):
    return (text or "").strip().lower() not in UNEXPLAINED


def _since(days):
    return (Date.fromisoformat(today()) - timedelta(days=days)).isoformat()


def gaps(since=None, until=None):
    """Liste de tous les écarts, du plus récent au plus ancien."""
    items = []
    if can("matieres"):
        for kind in ("inventaire", "perte"):
            for m in moves_with_balance("material", move_kind=kind, since=since, until=until):
                items.append({
                    "domain": "matiere", "date": m["date"], "name": m["item_name"],
                    "what": "Écart d'inventaire" if kind == "inventaire" else "Perte déclarée",
                    "before": m["before"], "after": m["after"], "qty": m["quantity"], "unit": m["unit"],
                    "value": m["quantity"] * (m["unit_cost"] or 0), "why": m["notes"] or "",
                    "by": m["full_name"] or m["username"], "table": "stock_moves", "id": m["id"],
                    "url": url_for("matieres.material", material_id=m["item_id"]), "module": "matieres",
                })
    if can("provenderie"):
        for kind in ("inventaire", "perte"):
            for m in moves_with_balance("feed", move_kind=kind, since=since, until=until):
                items.append({
                    "domain": "provende", "date": m["date"], "name": m["item_name"],
                    "what": "Écart d'inventaire" if kind == "inventaire" else "Perte déclarée",
                    "before": m["before"], "after": m["after"], "qty": m["quantity"], "unit": "kg",
                    "value": m["quantity"] * (m["unit_cost"] or 0), "why": m["notes"] or "",
                    "by": m["full_name"] or m["username"], "table": "feed_moves", "id": m["id"],
                    "url": url_for("provenderie.feed_history", formula_id=m["item_id"]), "module": "provenderie",
                })
    if can("lots"):
        where, params = ["e.kind IN ('comptage', 'perte')"], []
        if since:
            where.append("e.date >= ?")
            params.append(since)
        if until:
            where.append("e.date <= ?")
            params.append(until)
        rows = query(f"""SELECT e.*, l.name AS lot, l.initial_count, l.chick_price, u.username, u.full_name,
                                l.initial_count + COALESCE((SELECT SUM(x.quantity) FROM lot_events x
                                    WHERE x.lot_id = e.lot_id AND x.id < e.id), 0) AS before
                         FROM lot_events e JOIN lots l ON l.id = e.lot_id LEFT JOIN users u ON u.id = e.created_by
                         WHERE {' AND '.join(where)}""", params)
        for e in rows:
            items.append({
                "domain": "poules", "date": e["date"], "name": e["lot"],
                "what": "Écart de comptage" if e["kind"] == "comptage" else "Disparition / vol",
                "before": e["before"], "after": e["before"] + e["quantity"], "qty": e["quantity"], "unit": "poules",
                "value": e["quantity"] * (e["chick_price"] or 0), "why": e["notes"] or "",
                "by": e["full_name"] or e["username"], "table": "lot_events", "id": e["id"],
                "url": url_for("lots.lot", lot_id=e["lot_id"]) + "#effectif", "module": "lots",
            })
        items.extend(mortality_alerts(since, until))
    items.sort(key=lambda i: (i["date"], i.get("id", 0)), reverse=True)
    for item in items:
        item["explained"] = _explained(item["why"])
    return items


def mortality_alerts(since=None, until=None):
    """Jours où la mortalité d'un lot est anormale (plus de 3 fois sa moyenne, et au moins 3 morts)."""
    out = []
    for lot in query("SELECT id, name, chick_price FROM lots"):
        days = query("""SELECT e.date, -SUM(e.quantity) AS n, GROUP_CONCAT(e.notes, ' · ') AS notes, MAX(e.id) AS last_id
                        FROM lot_events e WHERE e.lot_id = ? AND e.kind = 'mort' GROUP BY e.date ORDER BY e.date""",
                     (lot["id"],))
        if not days:
            continue
        total = sum(d["n"] for d in days)
        first = Date.fromisoformat(days[0]["date"])
        span = max(7, (Date.fromisoformat(today()) - first).days + 1)
        avg = total / span
        for d in days:
            if d["n"] >= 3 and d["n"] > 3 * avg and (not since or d["date"] >= since) and (not until or d["date"] <= until):
                notes = (d["notes"] or "").strip(" ·")
                out.append({
                    "domain": "poules", "date": d["date"], "name": lot["name"], "what": "Mortalité anormale",
                    "before": None, "after": None, "qty": -d["n"], "unit": "poules",
                    "value": -d["n"] * (lot["chick_price"] or 0), "why": notes,
                    "by": f"moyenne {avg:.1f} / jour", "table": None, "id": d["last_id"],
                    "url": url_for("lots.lot", lot_id=lot["id"]) + "#effectif", "module": "lots",
                })
    return out


def summary(days=30):
    items = gaps(since=_since(days))
    by = {}
    for key, (emoji, label) in DOMAINS.items():
        sub = [i for i in items if i["domain"] == key]
        by[key] = {"emoji": emoji, "label": label, "count": len(sub),
                   "loss": sum(i["value"] for i in sub if i["value"] < 0),
                   "unexplained": sum(1 for i in sub if not i["explained"])}
    return {"items": items, "by": by, "unexplained": sum(1 for i in items if not i["explained"]),
            "loss": sum(i["value"] for i in items if i["value"] < 0)}


@bp.route("/")
@require("controles", VIEW)
def index():
    period = request.args.get("periode", "30")
    du, au = request.args.get("du", ""), request.args.get("au", "")
    if valid_date(du) or valid_date(au):
        items = gaps(since=du if valid_date(du) else None, until=au if valid_date(au) else None)
        period = "dates"
    else:
        days = {"7": 7, "30": 30, "90": 90, "365": 365}.get(period)
        items = gaps(since=_since(days) if days else None)
    domain = request.args.get("domaine", "")
    if domain:
        items = [i for i in items if i["domain"] == domain]
    if request.args.get("sans_explication"):
        items = [i for i in items if not i["explained"]]
    totals = {}
    for key, (emoji, label) in DOMAINS.items():
        sub = [i for i in items if i["domain"] == key]
        totals[key] = {"emoji": emoji, "label": label, "count": len(sub),
                       "loss": sum(i["value"] for i in sub if i["value"] < 0),
                       "unexplained": sum(1 for i in sub if not i["explained"])}
    return render_template("controles/index.html", items=items, totals=totals, period=period, domain=domain,
                           du=du, au=au, unexplained=sum(1 for i in items if not i["explained"]),
                           loss=sum(i["value"] for i in items if i["value"] < 0))


TABLES = {"stock_moves": "matieres", "feed_moves": "provenderie", "lot_events": "lots"}


@bp.route("/expliquer", methods=["POST"])
@login_required
def explain():
    table = request.form.get("table", "")
    row_id = request.form.get("id", type=int)
    text = " ".join(request.form.get("why", "").split())[:150]
    if table not in TABLES or not row_id or not can(TABLES[table], EDIT):
        abort(403)
    if not text:
        flash("Écrivez une explication.", "error")
    else:
        row = query(f"SELECT * FROM {table} WHERE id = ?", (row_id,), one=True)
        if row is None:
            abort(404)
        execute(f"UPDATE {table} SET notes = ? WHERE id = ?", (text, row_id))
        log_activity("Explication d'écart ajoutée", f"{table} n°{row_id} : {text}")
        ref = {"stock_moves": ("material", row["material_id"] if "material_id" in row.keys() else None),
               "feed_moves": ("formula", row["formula_id"] if "formula_id" in row.keys() else None),
               "lot_events": ("lot", row["lot_id"] if "lot_id" in row.keys() else None)}[table]
        notify(ref[0], ref[1], f"📝 Explication ajoutée pour l'écart du {date_fr(row['date'])} "
               f"({fmt_qty(row['quantity'])}) : « {text} »")
        flash("Explication enregistrée. Vos associés sont prévenus.", "success")
    back = request.form.get("next") or url_for("controles.index")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("controles.index")
    return redirect(back)


__all__ = ["bp", "summary", "gaps", "fmt_money", "g"]
