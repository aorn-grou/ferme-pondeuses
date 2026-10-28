"""Fumier (crottes de poules).

1. On enlève le fumier d'un lot : on note la date, le lot et la quantité (sacs, charrettes…).
2. Il sèche : il est prêt à vendre après 14 jours (modifiable).
3. On le vend : l'argent entre dans la caisse, et la vente compte dans les ventes du lot.
Le stock de fumier = ce qui a été enlevé − ce qui a été vendu, tas par tas.
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, require
from .stock import accounts, parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("fumier", __name__, url_prefix="/fumier")

UNITS = ["sac", "charrette", "kg", "tas"]
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _d(value):
    return Date.fromisoformat(value)


def batches(include_empty=True):
    rows = query("""SELECT b.*, l.name AS lot, u.username,
                           COALESCE((SELECT SUM(s.quantity) FROM sales s WHERE s.manure_id = b.id), 0) AS sold,
                           COALESCE((SELECT SUM(s.total) FROM sales s WHERE s.manure_id = b.id), 0) AS money
                    FROM manure_batches b LEFT JOIN lots l ON l.id = b.lot_id LEFT JOIN users u ON u.id = b.created_by
                    ORDER BY b.date DESC, b.id DESC""")
    out = []
    now = _d(today())
    for r in rows:
        x = dict(r)
        ready = _d(x["date"]) + timedelta(days=x["dry_days"] or 0)
        x["ready_date"] = ready.isoformat()
        x["left"] = max(0.0, (x["quantity"] or 0) - (x["sold"] or 0))
        x["days_left"] = (ready - now).days
        if x["left"] <= 1e-9:
            x["state"] = "vendu"
        elif x["days_left"] > 0:
            x["state"] = "seche"
        else:
            x["state"] = "pret"
        x["progress"] = 100 if x["state"] != "seche" else max(0, min(100, round(100 - x["days_left"] * 100 / max(1, x["dry_days"]))))
        if include_empty or x["state"] != "vendu":
            out.append(x)
    return out


def alerts():
    """Fumier prêt à vendre (le jour où il est sec, et tant qu'il en reste)."""
    if not can("fumier"):
        return []
    ready = [b for b in batches(include_empty=False) if b["state"] == "pret"]
    if not ready:
        return []
    by_unit = {}
    for b in ready:
        by_unit[b["unit"]] = by_unit.get(b["unit"], 0) + b["left"]
    what = ", ".join(f"{q:g} {u}{'s' if q > 1 and not u.endswith('s') and u != 'kg' else ''}" for u, q in by_unit.items())
    return [{"level": "info", "title": "Fumier prêt à vendre", "text": f"{what} de fumier sec, prêt à vendre.",
             "url": url_for("fumier.index") + "#tas", "module": "fumier"}]


@bp.route("/")
@require("fumier", VIEW)
def index():
    items = batches()
    month = today()[:7]
    sales = query("""SELECT s.*, c.name AS client, l.name AS lot FROM sales s LEFT JOIN clients c ON c.id = s.client_id
                     LEFT JOIN lots l ON l.id = s.lot_id WHERE s.manure_id IS NOT NULL ORDER BY s.date DESC, s.id DESC""")
    drying = {}
    ready = {}
    for b in items:
        if b["state"] == "seche":
            drying[b["unit"]] = drying.get(b["unit"], 0) + b["left"]
        elif b["state"] == "pret":
            ready[b["unit"]] = ready.get(b["unit"], 0) + b["left"]
    kpi = {
        "drying": drying, "ready": ready,
        "month_money": sum(s["total"] for s in sales if s["date"][:7] == month),
        "total_money": sum(s["total"] for s in sales),
        "next": min((b for b in items if b["state"] == "seche"), key=lambda b: b["ready_date"], default=None),
    }
    # par mois : enlevé et vendu (12 derniers mois)
    end = _d(today())
    keys = []
    y, m = end.year, end.month
    for _ in range(12):
        keys.insert(0, f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    per = {k: {"in": 0.0, "out": 0.0, "money": 0.0} for k in keys}
    for b in items:
        if b["date"][:7] in per:
            per[b["date"][:7]]["in"] += b["quantity"]
    for s in sales:
        if s["date"][:7] in per:
            per[s["date"][:7]]["out"] += s["quantity"]
            per[s["date"][:7]]["money"] += s["total"]
    first = next((k for k in keys if per[k]["in"] or per[k]["out"]), None)
    shown = [k for k in keys if first and k >= first]
    chart = {"labels": [f"{MONTHS[int(k[5:7]) - 1]} {k[2:4]}" for k in shown], "bars": [round(per[k]["money"]) for k in shown],
             "line": [None] * len(shown), "unit": "Ar", "barColor": "#8d6e3f"} if shown else None
    months = [{"key": k, "label": f"{MONTHS[int(k[5:7]) - 1]} {k[:4]}", **per[k]} for k in reversed(shown)]
    lots = query("SELECT * FROM lots WHERE status = 'actif' ORDER BY name")
    sellable = [b for b in items if b["state"] == "pret"]
    return render_template("fumier/index.html", items=items, sales=sales, kpi=kpi, chart=chart, months=months, lots=lots,
                           units=UNITS, today=today(), sellable=sellable, accounts=accounts(),
                           clients=query("SELECT * FROM clients ORDER BY name"),
                           last_price=query("SELECT unit_price, unit FROM sales WHERE manure_id IS NOT NULL ORDER BY id DESC LIMIT 1", one=True))


@bp.route("/enlever", methods=["POST"])
@require("fumier", EDIT)
def remove():
    f = request.form
    day = f.get("date") or today()
    qty = parse_num(f.get("quantity"))
    unit = " ".join(f.get("unit", "sac").split())[:20] or "sac"
    dry = parse_num(f.get("dry_days"), 14)
    lot_id = f.get("lot_id") or None
    notes = " ".join(f.get("notes", "").split())[:150]
    back = redirect(url_for("fumier.index"))
    if not valid_date(day) or day > today():
        flash("Date invalide (pas de date dans le futur).", "error")
        return back
    if qty is None or qty <= 0:
        flash("Écrivez la quantité de fumier enlevée.", "error")
        return back
    if dry is None or dry < 0 or dry > 365:
        dry = 14
    lot = query("SELECT * FROM lots WHERE id = ?", (lot_id or 0,), one=True) if lot_id else None
    new_id = execute("""INSERT INTO manure_batches (date, lot_id, quantity, unit, dry_days, notes, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (day, lot["id"] if lot else None, qty, unit, int(dry), notes, g.user["id"], now_utc()))
    ready = (_d(day) + timedelta(days=int(dry))).isoformat()
    log_activity("Fumier enlevé", f"{qty:g} {unit}" + (f" — {lot['name']}" if lot else "") + f", prêt le {date_fr(ready)}")
    notify("", None, f"🌱 Fumier enlevé le {date_fr(day)}" + (f" (lot « {lot['name']} »)" if lot else "") +
           f" : {qty:g} {unit}. Prêt à vendre le {date_fr(ready)}.")
    flash(f"Fumier noté : {qty:g} {unit}. Il sera prêt à vendre le {date_fr(ready)} : une alerte vous préviendra.", "success")
    return redirect(url_for("fumier.index") + f"#tas-{new_id}")


@bp.route("/vendre", methods=["POST"])
@require("fumier", EDIT)
def sell():
    f = request.form
    batch = next((b for b in batches() if str(b["id"]) == (f.get("batch_id") or "")), None)
    day = f.get("date") or today()
    qty = parse_num(f.get("quantity"))
    price = parse_num(f.get("price"), 0) or 0
    back = redirect(url_for("fumier.index") + "#vendre")
    if batch is None:
        flash("Choisissez le tas de fumier à vendre.", "error")
        return back
    if not valid_date(day) or day > today():
        flash("Date invalide.", "error")
        return back
    if day < batch["ready_date"] and not f.get("force"):
        flash(f"Ce fumier n'est pas encore sec : il sera prêt le {date_fr(batch['ready_date'])}.", "error")
        return back
    if qty is None or qty <= 0:
        flash("Écrivez la quantité vendue.", "error")
        return back
    if qty > batch["left"] + 1e-9:
        flash(f"Il ne reste que {batch['left']:g} {batch['unit']} dans ce tas.", "error")
        return back
    total = round(qty * price, 2) if f.get("price_mode", "unite") == "unite" else price
    unit_price = total / qty
    paid = parse_num(f.get("paid"))
    paid = total if paid is None else paid
    if total < 0 or paid < 0 or paid > total + 0.5:
        flash("Le montant payé doit être entre 0 et le total.", "error")
        return back
    account = query("SELECT * FROM accounts WHERE id = ?", (f.get("account_id") or 0,), one=True)
    if paid > 0 and account is None:
        flash("Choisissez la caisse où va l'argent, ou écrivez 0 dans « Payé ».", "error")
        return back
    client_id = f.get("client_id") or None
    notes = " ".join(f.get("notes", "").split())[:200]
    with transaction() as conn:
        cur = conn.execute("""INSERT INTO sales (date, client_id, product, is_eggs, quantity, unit, input_qty, unit_price, total, paid,
                              account_id, lot_id, notes, manure_id, created_by, created_at)
                              VALUES (?, ?, 'Fumier', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (day, client_id, qty, batch["unit"], f"{qty:g} {batch['unit']}", unit_price, total, paid,
                            account["id"] if account and paid > 0 else None, batch["lot_id"], notes, batch["id"],
                            g.user["id"], now_utc()))
        sale_id = cur.lastrowid
        if paid > 0:
            conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                            VALUES (?, ?, ?, 'vente', ?, 'sale', ?, ?, ?)""",
                         (day, account["id"], paid, f"Vente de fumier n°{sale_id}", sale_id, g.user["id"], now_utc()))
    log_activity("Vente de fumier", f"n°{sale_id} : {qty:g} {batch['unit']} = {fmt_money(total)}")
    notify("sale", sale_id, f"💰 Fumier vendu : {qty:g} {batch['unit']} = {fmt_money(total)}"
           + (f" · reste {fmt_money(total - paid)} à recevoir" if total - paid > 0.5 else " · payé"))
    flash(f"Vente de fumier enregistrée : {qty:g} {batch['unit']} = {fmt_money(total)}."
          + (f" L'argent ({fmt_money(paid)}) est entré dans « {account['name']} »." if paid > 0 else " À crédit."), "success")
    return redirect(url_for("fumier.index") + "#ventes")


@bp.route("/<int:batch_id>/supprimer", methods=["POST"])
@require("fumier", MANAGE)
def delete(batch_id):
    row = query("SELECT * FROM manure_batches WHERE id = ?", (batch_id,), one=True)
    if row is None:
        abort(404)
    if query("SELECT 1 FROM sales WHERE manure_id = ?", (batch_id,), one=True):
        flash("Ce tas a déjà des ventes : annulez d'abord ses ventes (dans Ventes), puis supprimez-le.", "error")
        return redirect(url_for("fumier.index"))
    execute("DELETE FROM manure_batches WHERE id = ?", (batch_id,))
    log_activity("Fumier supprimé", f"{date_fr(row['date'])} : {row['quantity']:g} {row['unit']}")
    flash("Enlèvement de fumier supprimé.", "success")
    return redirect(url_for("fumier.index"))


__all__ = ["bp", "alerts", "batches"]
