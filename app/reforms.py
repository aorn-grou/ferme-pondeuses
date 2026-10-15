"""Poules de réforme : quand vendre les vieilles poules, et la vente elle-même.

Le logiciel PROPOSE (il ne décide pas) :
- « À réformer » : le lot a atteint sa semaine de réforme prévue, ou sa ponte ne paie plus la provende
  (taux de ponte sous le seuil de rentabilité calculé dans Rapports).
- « À surveiller » : la réforme approche (4 semaines) ou la ponte est proche du seuil.
- « Rentable » : rien à faire.

Une vente de poules de réforme fait 3 choses d'un coup :
1. les poules sortent de l'effectif du lot,
2. une vente est créée (client, crédit éventuel) et l'argent entre dans la caisse,
3. le prix compte dans les ventes du lot (son bénéfice final).
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import now_utc, query, transaction
from .discussion import notify
from .security import EDIT, VIEW, can, require
from .stock import accounts, parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("reformes", __name__, url_prefix="/reformes")


def advice(lot):
    """Conseil pour un lot actif : état, raison, chiffres."""
    from .eggs import lot_egg_stats
    from .lots import effectif, lot_week
    from .reports import lot_report

    week = lot_week(lot)
    birds = effectif(lot["id"])
    eggs = lot_egg_stats(lot)
    rate = eggs["avg7"]
    lw = lot["laying_week"] or 18
    rw = lot["reform_week"] or 72
    threshold = None
    end_profit = None
    try:
        rep = lot_report(lot)
        threshold, end_profit = rep["threshold"], rep["end_profit"]
    except Exception:  # un calcul impossible (pas de prix…) ne doit pas bloquer la page
        pass
    reasons = []
    state = "ok"
    if week >= rw:
        state = "now"
        reasons.append(f"il a atteint la semaine de réforme prévue (S{rw})")
    elif week >= rw - 4:
        state = "watch"
        reasons.append(f"la réforme prévue (S{rw}) arrive dans {rw - week} semaine(s)")
    if rate is not None and threshold and week >= lw + 10:
        if rate < threshold:
            state = "now"
            reasons.append(f"sa ponte ({rate:.0f} %) est sous le seuil qui paie la provende ({threshold:.0f} %)")
        elif rate < threshold + 5 and state == "ok":
            state = "watch"
            reasons.append(f"sa ponte ({rate:.0f} %) est proche du seuil de rentabilité ({threshold:.0f} %)")
    if not reasons:
        reasons.append("sa ponte paie la provende et la semaine de réforme est encore loin" if week >= lw
                       else "les poules ne pondent pas encore")
    return {"lot": lot, "week": week, "birds": birds, "rate": rate, "threshold": threshold, "reform_week": rw,
            "state": state, "reasons": reasons, "end_profit": end_profit}


def alerts():
    out = []
    for lot in query("SELECT * FROM lots WHERE status = 'actif'"):
        from .lots import lot_week

        if lot_week(lot) >= (lot["reform_week"] or 72):
            out.append({"level": "warning", "title": f"🏷️ Lot « {lot['name']} » : semaine de réforme atteinte",
                        "text": "Pensez à vendre les poules de réforme (ou changez la semaine de réforme du lot).",
                        "url": url_for("reformes.index") + f"#lot-{lot['id']}"})
    return out


def reform_sales():
    return query("""SELECT s.*, c.name AS client, l.name AS lot,
                           s.paid + COALESCE((SELECT SUM(p.amount) FROM sale_payments p WHERE p.sale_id = s.id), 0) AS paid_total
                    FROM sales s LEFT JOIN clients c ON c.id = s.client_id LEFT JOIN lots l ON l.id = s.lot_id
                    WHERE s.lot_event_id IS NOT NULL ORDER BY s.date DESC, s.id DESC""")


@bp.route("/")
@require("reformes", VIEW)
def index():
    lots = query("SELECT * FROM lots WHERE status = 'actif' ORDER BY arrival_date")
    items = [advice(l) for l in lots] if can("lots") or can("reformes") else []
    sales = reform_sales()
    tot = {"birds": sum(s["quantity"] for s in sales), "total": sum(s["total"] for s in sales),
           "due": sum(max(0, s["total"] - s["paid_total"]) for s in sales)}
    last = sales[0] if sales else None
    return render_template("reformes/index.html", items=items, sales=sales, tot=tot, lots=lots, accounts=accounts(), today=today(),
                           clients=query("SELECT * FROM clients ORDER BY name"), last=last)


@bp.route("/vendre", methods=["POST"])
@require("reformes", EDIT)
def sell():
    from .lots import effectif

    f = request.form
    lot = query("SELECT * FROM lots WHERE id = ?", (f.get("lot_id") or 0,), one=True)
    date = f.get("date") or today()
    n = parse_num(f.get("birds"))
    price = parse_num(f.get("price"), 0) or 0
    mode = f.get("price_mode", "poule")
    weight = parse_num(f.get("weight"), 0) or 0
    back = redirect(url_for("reformes.index") + "#vendre")
    if lot is None:
        flash("Choisissez le lot.", "error")
        return back
    if not valid_date(date) or date > today() or date < lot["arrival_date"]:
        flash("Date invalide.", "error")
        return back
    if n is None or n <= 0 or int(n) != n:
        flash("Écrivez le nombre de poules vendues (un nombre entier).", "error")
        return back
    n = int(n)
    have = effectif(lot["id"], date)
    if n > have:
        flash(f"Impossible : le lot « {lot['name']} » n'a que {have} poules au {date_fr(date)}.", "error")
        return back
    if mode == "kg":
        if weight <= 0:
            flash("Écrivez le poids total vendu (kg).", "error")
            return back
        total = weight * price
        written = f"{n} poules ({weight:g} kg)"
        unit_price = total / n
    elif mode == "total":
        total = price
        written = f"{n} poules"
        unit_price = total / n
    else:
        total = n * price
        written = f"{n} poules"
        unit_price = price
    paid = parse_num(f.get("paid"))
    paid = total if paid is None else paid
    account = query("SELECT * FROM accounts WHERE id = ?", (f.get("account_id") or 0,), one=True)
    if total < 0 or paid < 0 or paid > total + 0.5:
        flash("Le montant payé doit être entre 0 et le total.", "error")
        return back
    if paid > 0 and account is None:
        flash("Choisissez où va l'argent, ou écrivez 0 dans « Payé ».", "error")
        return back
    client_id = f.get("client_id") or None
    notes = " ".join(f.get("notes", "").split())[:200]
    with transaction() as conn:
        ev = conn.execute("""INSERT INTO lot_events (lot_id, date, kind, quantity, notes, created_by, created_at)
                             VALUES (?, ?, 'reforme', ?, ?, ?, ?)""", (lot["id"], date, -n, "Vente de réforme", g.user["id"], now_utc()))
        cur = conn.execute("""INSERT INTO sales (date, client_id, product, is_eggs, quantity, unit, input_qty, unit_price, total, paid,
                              account_id, lot_id, notes, lot_event_id, created_by, created_at)
                              VALUES (?, ?, 'Poules de réforme', 0, ?, 'poule', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (date, client_id, n, written, unit_price, total, paid, account["id"] if account and paid > 0 else None,
                            lot["id"], notes, ev.lastrowid, g.user["id"], now_utc()))
        sale_id = cur.lastrowid
        conn.execute("UPDATE lot_events SET notes = ? WHERE id = ?", (f"Vente de réforme n°{sale_id}", ev.lastrowid))
        if paid > 0:
            conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                            VALUES (?, ?, ?, 'vente', ?, 'sale', ?, ?, ?)""",
                         (date, account["id"], paid, f"Vente réforme n°{sale_id}", sale_id, g.user["id"], now_utc()))
        left = have - n
        closed = False
        if left == 0 and f.get("close") and lot["status"] == "actif":
            conn.execute("UPDATE lots SET status = 'termine', end_date = ? WHERE id = ?", (date, lot["id"]))
            conn.execute("DELETE FROM health_events WHERE lot_id = ? AND status = 'prevu'", (lot["id"],))
            closed = True
    client = query("SELECT name FROM clients WHERE id = ?", (client_id or 0,), one=True)
    log_activity("Vente de poules de réforme", f"{lot['name']} : {n} poules = {fmt_money(total)}")
    notify("lot", lot["id"], f"🏷️ Réforme — lot « {lot['name']} » : {n} poules vendues" + (f" à {client['name']}" if client else "")
           + f" = {fmt_money(total)} ({fmt_money(unit_price)} la poule)" + (f" · reste {fmt_money(total - paid)} à recevoir" if total - paid > 0.5 else "")
           + f". Effectif : {have} → {left}." + (" Le lot est terminé." if closed else ""))
    flash(f"Vente enregistrée : {n} poules = {fmt_money(total)}. Il reste {left} poule(s) dans le lot."
          + (" Le lot est terminé : son bilan final est dans Rapports." if closed else ""), "success")
    return redirect(url_for("ventes.sale", sale_id=sale_id) if f.get("go_sale") else url_for("reformes.index") + "#ventes")


__all__ = ["bp", "advice", "alerts"]
