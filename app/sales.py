"""Ventes & clients, et stock d'œufs.

Stock d'œufs = bons œufs ramassés − œufs vendus ± corrections (inventaire, casse, œufs consommés…).
Une vente peut être payée tout de suite, en partie ou à crédit : le reste à recevoir est suivi par client.
Tout s'écrit librement : un client inconnu est créé, un produit autre que les œufs (fumier, poules…) est accepté.
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, can_correct, require
from .stock import accounts, parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("ventes", __name__, url_prefix="/ventes")

TRAY = 30
MOVE_KINDS = {
    "inventaire": "Inventaire (comptage)",
    "casse": "Œufs cassés au stock",
    "perte": "Perte / vol",
    "consommation": "Œufs consommés / donnés",
    "ajout": "Ajout (œufs retrouvés…)",
}


def _d(value):
    return Date.fromisoformat(value)


def _label(d):
    return f"{d[8:10]}/{d[5:7]}"


def is_egg_product(name):
    n = (name or "").strip().lower()
    return n.startswith("œuf") or n.startswith("oeuf") or n.startswith("œufs") or n == ""


def fmt_eggs(n):
    """« 452 œufs (15 plateaux + 2) »"""
    n = int(round(n or 0))
    trays, rest = divmod(abs(n), TRAY)
    sign = "−" if n < 0 else ""
    if trays:
        return f"{sign}{abs(n):,} œufs ({trays} plateau{'x' if trays > 1 else ''}{f' + {rest}' if rest else ''})".replace(",", " ")
    return f"{sign}{abs(n)} œuf{'s' if abs(n) > 1 else ''}"


# ---------------------------------------------------------------------------
# Stock d'œufs
# ---------------------------------------------------------------------------
def egg_stock(until=None):
    p = (until,) if until else ()
    w = " WHERE date <= ?" if until else ""
    good = query(f"SELECT COALESCE(SUM(good), 0) AS n FROM egg_collections{w}", p, one=True)["n"]
    sold = query(f"SELECT COALESCE(SUM(quantity), 0) AS n FROM sales WHERE is_eggs = 1{' AND date <= ?' if until else ''}",
                 p, one=True)["n"]
    moves = query(f"SELECT COALESCE(SUM(quantity), 0) AS n FROM egg_moves{w}", p, one=True)["n"]
    return int(round(good - sold + moves))


def egg_ledger(since=None):
    """Tous les mouvements du stock d'œufs, avec le stock avant et après chacun."""
    rows = []
    for c in query("SELECT e.*, l.name AS lot, u.username FROM egg_collections e JOIN lots l ON l.id = e.lot_id "
                   "LEFT JOIN users u ON u.id = e.created_by"):
        rows.append({"date": c["date"], "order": 0, "id": c["id"], "kind": "ramassage", "qty": c["good"],
                     "label": f"Ramassage {c['lot']}", "notes": c["notes"] or "", "by": c["username"],
                     "url": url_for("oeufs.index", date=c["date"])})
    for s in query("SELECT s.*, c.name AS client, u.username FROM sales s LEFT JOIN clients c ON c.id = s.client_id "
                   "LEFT JOIN users u ON u.id = s.created_by WHERE s.is_eggs = 1"):
        rows.append({"date": s["date"], "order": 1, "id": s["id"], "kind": "vente", "qty": -s["quantity"],
                     "label": f"Vente n°{s['id']}" + (f" · {s['client']}" if s["client"] else ""), "notes": s["notes"] or "",
                     "by": s["username"], "url": url_for("ventes.sale", sale_id=s["id"])})
    for m in query("SELECT m.*, u.username FROM egg_moves m LEFT JOIN users u ON u.id = m.created_by"):
        rows.append({"date": m["date"], "order": 2, "id": m["id"], "kind": m["kind"], "qty": m["quantity"],
                     "label": MOVE_KINDS.get(m["kind"], m["kind"]), "notes": m["notes"] or "", "by": m["username"],
                     "url": None, "move": m})
    rows.sort(key=lambda r: (r["date"], r["order"], r["id"]))
    stock = 0
    for r in rows:
        r["before"] = stock
        stock += r["qty"]
        r["after"] = stock
    if since:
        rows = [r for r in rows if r["date"] >= since]
    return rows


def stock_series(days=30):
    end = _d(today())
    keys = [(end - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    first = query("""SELECT MIN(d) AS d FROM (SELECT MIN(date) AS d FROM egg_collections UNION ALL
                     SELECT MIN(date) FROM sales WHERE is_eggs = 1 UNION ALL SELECT MIN(date) FROM egg_moves)""", one=True)["d"]
    if first:
        keys = [k for k in keys if k >= first] or keys[-1:]
    return {"labels": [_label(k) for k in keys], "values": [max(0, egg_stock(k)) for k in keys], "unit": "œufs",
            "color": "#c9971c"}


def egg_move_gaps(since=None, until=None):
    """Écarts du stock d'œufs (inventaire, perte, casse) pour « Écarts & contrôles »."""
    out = []
    price = query("SELECT AVG(unit_price) AS p FROM sales WHERE is_eggs = 1 AND unit_price > 0", one=True)["p"] or \
        query("SELECT AVG(egg_price) AS p FROM lots WHERE egg_price > 0", one=True)["p"] or 0
    for r in egg_ledger():
        if r["kind"] not in ("inventaire", "perte", "casse") or not r["qty"]:
            continue
        if (since and r["date"] < since) or (until and r["date"] > until):
            continue
        out.append({
            "domain": "oeufs", "date": r["date"], "name": "Stock d'œufs",
            "what": "Écart d'inventaire" if r["kind"] == "inventaire" else MOVE_KINDS[r["kind"]],
            "before": r["before"], "after": r["after"], "qty": r["qty"], "unit": "œufs",
            "value": r["qty"] * price, "why": r["notes"] if r["notes"] != "inventaire physique" else "",
            "by": r["by"], "table": "egg_moves", "id": r["id"], "url": url_for("ventes.stock"), "module": "ventes",
        })
    return out


# ---------------------------------------------------------------------------
# Ventes, paiements, clients
# ---------------------------------------------------------------------------
def sale_paid(sale):
    later = query("SELECT COALESCE(SUM(amount), 0) AS a FROM sale_payments WHERE sale_id = ?", (sale["id"],), one=True)["a"]
    return (sale["paid"] or 0) + later


def sales_rows(since=None, until=None, client_id=None):
    where, params = [], []
    if since:
        where.append("s.date >= ?")
        params.append(since)
    if until:
        where.append("s.date <= ?")
        params.append(until)
    if client_id:
        where.append("s.client_id = ?")
        params.append(client_id)
    rows = query(f"""SELECT s.*, c.name AS client, l.name AS lot, u.username,
                            s.paid + COALESCE((SELECT SUM(p.amount) FROM sale_payments p WHERE p.sale_id = s.id), 0) AS paid_total
                     FROM sales s LEFT JOIN clients c ON c.id = s.client_id LEFT JOIN lots l ON l.id = s.lot_id
                     LEFT JOIN users u ON u.id = s.created_by
                     {('WHERE ' + ' AND '.join(where)) if where else ''} ORDER BY s.date DESC, s.id DESC""", params)
    return [dict(r, due=max(0, r["total"] - r["paid_total"])) for r in rows]


def client_balance(client_id):
    rows = sales_rows(client_id=client_id)
    return {"bought": sum(r["total"] for r in rows), "paid": sum(r["paid_total"] for r in rows),
            "due": sum(r["due"] for r in rows), "count": len(rows)}


def total_due():
    return sum(r["due"] for r in sales_rows())


def lot_revenue(lot):
    """Ventes d'œufs attribuées à un lot : ventes marquées pour ce lot + sa part des ventes sans lot
    (au prorata des bons œufs qu'il a ramassés)."""
    direct = query("SELECT COALESCE(SUM(total), 0) AS t FROM sales WHERE is_eggs = 1 AND lot_id = ?", (lot["id"],), one=True)["t"]
    pool = query("SELECT COALESCE(SUM(total), 0) AS t FROM sales WHERE is_eggs = 1 AND lot_id IS NULL", one=True)["t"]
    mine = query("SELECT COALESCE(SUM(good), 0) AS n FROM egg_collections WHERE lot_id = ?", (lot["id"],), one=True)["n"]
    allg = query("SELECT COALESCE(SUM(good), 0) AS n FROM egg_collections", one=True)["n"]
    share = (mine / allg) if allg else 0
    return {"direct": direct, "shared": pool * share, "total": direct + pool * share, "share": share * 100}


def _period_days(n):
    return (_d(today()) - timedelta(days=n - 1)).isoformat()


@bp.route("/")
@require("ventes", VIEW)
def index():
    period = request.args.get("periode", "30")
    days = {"7": 7, "30": 30, "90": 90, "365": 365}.get(period)
    rows = sales_rows(since=_period_days(days) if days else None)
    month = today()[:7]
    m_rows = [r for r in sales_rows(since=month + "-01")]
    in_month = (query("SELECT COALESCE(SUM(paid), 0) AS a FROM sales WHERE substr(date, 1, 7) = ?", (month,), one=True)["a"]
                + query("SELECT COALESCE(SUM(amount), 0) AS a FROM sale_payments WHERE substr(date, 1, 7) = ?", (month,),
                        one=True)["a"])
    eggs_month = sum(r["quantity"] for r in m_rows if r["is_eggs"])
    egg_money = sum(r["total"] for r in m_rows if r["is_eggs"])
    kpi = {"ca": sum(r["total"] for r in m_rows), "cashed": in_month, "due": total_due(), "eggs": eggs_month,
           "price": (egg_money / eggs_month) if eggs_month else None, "stock": egg_stock()}
    # graphique : ventes par jour (30 j)
    end = _d(today())
    keys = [(end - timedelta(days=29 - i)).isoformat() for i in range(30)]
    per = {r["d"]: r for r in query("SELECT date AS d, SUM(total) AS t, SUM(CASE WHEN is_eggs = 1 THEN quantity ELSE 0 END) AS q "
                                    "FROM sales WHERE date >= ? GROUP BY date", (keys[0],))}
    if per:  # on commence au premier jour de vente (au moins 7 jours affichés)
        first = min(per)
        keys = [k for k in keys if k >= first] if len([k for k in keys if k >= first]) >= 7 else keys[-7:]
    chart = {"labels": [_label(k) for k in keys], "bars": [round(per[k]["q"]) if k in per else 0 for k in keys],
             "line": [round(per[k]["t"]) if k in per else None for k in keys], "unit": "œufs", "barLabel": "Œufs vendus",
             "lineLabel": "Montant", "lineUnit": "Ar"} if per else None
    return render_template("ventes/index.html", rows=rows, kpi=kpi, period=period, chart=chart, fmt_eggs=fmt_eggs)


def _form_values():
    f = request.form
    product = " ".join(f.get("product", "").split())[:60] or "Œufs"
    eggs = is_egg_product(product)
    if eggs:
        qty = (parse_num(f.get("trays"), 0) or 0) * TRAY + (parse_num(f.get("eggs"), 0) or 0)
        unit = "œuf"
        written = " + ".join(x for x in [f"{f.get('trays').strip()} plateau(x)" if (f.get("trays") or "").strip() else "",
                                         f"{f.get('eggs').strip()} œuf(s)" if (f.get("eggs") or "").strip() else ""] if x)
    else:
        qty = parse_num(f.get("qty"), 0) or 0
        unit = " ".join(f.get("unit", "").split())[:20] or "unité"
        written = f"{f.get('qty', '').strip()} {unit}"
    price = parse_num(f.get("price"), 0) or 0
    price_unit = f.get("price_unit", "oeuf")
    unit_price = price / TRAY if (eggs and price_unit == "plateau") else price
    total = round(qty * unit_price, 2)
    return {"date": f.get("date", "").strip(), "client_id": f.get("client_id") or None, "product": product, "is_eggs": 1 if eggs else 0,
            "quantity": qty, "unit": unit, "input_qty": written, "unit_price": unit_price, "price": price,
            "price_unit": price_unit, "total": total, "paid": parse_num(f.get("paid")), "account_id": f.get("account_id") or None,
            "lot_id": f.get("lot_id") or None, "notes": " ".join(f.get("notes", "").split())[:200],
            "trays": f.get("trays", ""), "eggs": f.get("eggs", ""), "qty": f.get("qty", "")}


@bp.route("/nouvelle", methods=["GET", "POST"])
@require("ventes", EDIT)
def sale_new():
    last = query("SELECT * FROM sales WHERE is_eggs = 1 AND unit_price > 0 ORDER BY id DESC LIMIT 1", one=True)
    default_price = (last["unit_price"] * TRAY if last else None) or \
        ((query("SELECT egg_price FROM lots WHERE egg_price > 0 ORDER BY id DESC LIMIT 1", one=True) or {"egg_price": 0})["egg_price"] * TRAY)
    values = {"date": today(), "client_id": None, "product": "Œufs", "trays": "", "eggs": "", "qty": "", "unit": "",
              "price": int(default_price) if default_price else "", "price_unit": "plateau", "paid": None,
              "account_id": None, "lot_id": None, "notes": ""}
    if request.method == "POST":
        values = _form_values()
        errors = []
        if not valid_date(values["date"]):
            errors.append("Date invalide.")
        if values["quantity"] <= 0:
            errors.append("Indiquez la quantité vendue.")
        if values["unit_price"] < 0:
            errors.append("Le prix ne peut pas être négatif.")
        if values["is_eggs"] and values["quantity"] > 0 and valid_date(values["date"]):
            stock = egg_stock(values["date"])
            if values["quantity"] > stock:
                errors.append(f"Il n'y a que {fmt_eggs(stock)} en stock au {date_fr(values['date'])}. "
                              "Saisissez d'abord le ramassage (menu Œufs), ou corrigez le stock dans « Stock d'œufs ».")
        paid = values["total"] if values["paid"] is None else values["paid"]
        if paid < 0 or paid > values["total"] + 0.5:
            errors.append("Le montant payé doit être entre 0 et le total de la vente.")
        account = query("SELECT * FROM accounts WHERE id = ?", (values["account_id"] or 0,), one=True)
        if paid > 0 and account is None:
            errors.append("Indiquez où va l'argent (caisse, mobile money, propriétaire…), ou écrivez 0 dans « Payé ».")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO sales (date, client_id, product, is_eggs, quantity, unit, input_qty, unit_price, total, paid,
                       account_id, lot_id, notes, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (values["date"], values["client_id"], values["product"], values["is_eggs"], values["quantity"], values["unit"],
                     values["input_qty"], values["unit_price"], values["total"], paid, account["id"] if account and paid > 0 else None,
                     values["lot_id"], values["notes"], g.user["id"], now_utc()))
                sale_id = cur.lastrowid
                if paid > 0:
                    conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                                    VALUES (?, ?, ?, 'vente', ?, 'sale', ?, ?, ?)""",
                                 (values["date"], account["id"], paid, f"Vente n°{sale_id}", sale_id, g.user["id"], now_utc()))
            client = query("SELECT name FROM clients WHERE id = ?", (values["client_id"] or 0,), one=True)
            what = fmt_eggs(values["quantity"]) if values["is_eggs"] else f"{values['input_qty']} de {values['product']}"
            due = values["total"] - paid
            log_activity("Vente enregistrée", f"n°{sale_id} : {what} = {fmt_money(values['total'])}")
            notify("sale", sale_id, f"💰 Vente n°{sale_id}{' à ' + client['name'] if client else ''} : {what} = "
                   f"{fmt_money(values['total'])}" + (f" · payé {fmt_money(paid)}, reste {fmt_money(due)} à recevoir" if due > 0.5 else " · payé")
                   + (f" « {values['notes']} »" if values["notes"] else ""))
            flash(f"Vente enregistrée : {what} = {fmt_money(values['total'])}." + (f" Reste à recevoir : {fmt_money(due)}." if due > 0.5 else ""),
                  "success")
            return redirect(url_for("ventes.sale", sale_id=sale_id))
    return render_template("ventes/sale_form.html", values=values, clients=query("SELECT * FROM clients ORDER BY name"),
                           accounts=accounts(), lots=query("SELECT * FROM lots WHERE status = 'actif' ORDER BY name"),
                           stock=egg_stock(), tray=TRAY,
                           products=[r["product"] for r in query("SELECT DISTINCT product FROM sales ORDER BY product")])


@bp.route("/<int:sale_id>")
@require("ventes", VIEW)
def sale(sale_id):
    rows = [r for r in sales_rows() if r["id"] == sale_id]
    if not rows:
        abort(404)
    item = rows[0]
    payments = query("""SELECT p.*, a.name AS account, u.username FROM sale_payments p LEFT JOIN accounts a ON a.id = p.account_id
                        LEFT JOIN users u ON u.id = p.created_by WHERE p.sale_id = ? ORDER BY p.date, p.id""", (sale_id,))
    account = query("SELECT name FROM accounts WHERE id = ?", (item["account_id"] or 0,), one=True)
    return render_template("ventes/sale.html", item=item, payments=payments, accounts=accounts(), today=today(),
                           account=account, fmt_eggs=fmt_eggs, raw=query("SELECT * FROM sales WHERE id = ?", (sale_id,), one=True))


@bp.route("/<int:sale_id>/encaisser", methods=["POST"])
@require("ventes", EDIT)
def payment_new(sale_id):
    rows = [r for r in sales_rows() if r["id"] == sale_id]
    if not rows:
        abort(404)
    item = rows[0]
    amount = parse_num(request.form.get("amount"), 0) or 0
    date = request.form.get("date") or today()
    account = query("SELECT * FROM accounts WHERE id = ?", (request.form.get("account_id") or 0,), one=True)
    if amount <= 0 or amount > item["due"] + 0.5:
        flash(f"Montant invalide : il reste {fmt_money(item['due'])} à recevoir.", "error")
    elif not valid_date(date) or account is None:
        flash("Indiquez la date et où va l'argent.", "error")
    else:
        with transaction() as conn:
            cur = conn.execute("INSERT INTO sale_payments (sale_id, date, amount, account_id, notes, created_by, created_at) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)", (sale_id, date, amount, account["id"],
                                                                " ".join(request.form.get("notes", "").split())[:150], g.user["id"], now_utc()))
            conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                            VALUES (?, ?, ?, 'vente', ?, 'sale_payment', ?, ?, ?)""",
                         (date, account["id"], amount, f"Paiement vente n°{sale_id}", cur.lastrowid, g.user["id"], now_utc()))
        rest = item["due"] - amount
        log_activity("Paiement de vente reçu", f"n°{sale_id} : {fmt_money(amount)}")
        notify("sale", sale_id, f"💵 Paiement reçu pour la vente n°{sale_id}{' (' + item['client'] + ')' if item['client'] else ''} : "
               f"{fmt_money(amount)} → {account['name']}" + (f" · reste {fmt_money(rest)}" if rest > 0.5 else " · tout est payé ✅"))
        flash("Paiement enregistré." + (f" Reste {fmt_money(rest)}." if rest > 0.5 else " La vente est entièrement payée."), "success")
    return redirect(url_for("ventes.sale", sale_id=sale_id))


@bp.route("/<int:sale_id>/annuler", methods=["POST"])
@require("ventes", EDIT)
def sale_delete(sale_id):
    item = query("SELECT * FROM sales WHERE id = ?", (sale_id,), one=True)
    if item is None:
        abort(404)
    if not can_correct("ventes", item):
        abort(403)
    with transaction() as conn:
        pay_ids = [r["id"] for r in conn.execute("SELECT id FROM sale_payments WHERE sale_id = ?", (sale_id,)).fetchall()]
        for pid in pay_ids:
            conn.execute("DELETE FROM cash_movements WHERE ref_type = 'sale_payment' AND ref_id = ?", (pid,))
        conn.execute("DELETE FROM sale_payments WHERE sale_id = ?", (sale_id,))
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'sale' AND ref_id = ?", (sale_id,))
        conn.execute("DELETE FROM sales WHERE id = ?", (sale_id,))
    what = fmt_eggs(item["quantity"]) if item["is_eggs"] else item["product"]
    log_activity("Vente annulée", f"n°{sale_id} : {what} = {fmt_money(item['total'])}")
    notify("", None, f"↩️ Vente n°{sale_id} annulée ({what}, {fmt_money(item['total'])}) : "
           + ("les œufs reviennent en stock, " if item["is_eggs"] else "") + "l'argent est retiré de la caisse.")
    flash("Vente annulée." + (" Les œufs sont revenus en stock." if item["is_eggs"] else ""), "success")
    return redirect(url_for("ventes.index"))


@bp.route("/clients")
@require("ventes", VIEW)
def clients():
    rows = []
    for c in query("SELECT * FROM clients ORDER BY name"):
        rows.append({"c": c, **client_balance(c["id"]),
                     "last": query("SELECT MAX(date) AS d FROM sales WHERE client_id = ?", (c["id"],), one=True)["d"]})
    rows.sort(key=lambda r: (-r["due"], -r["bought"]))
    return render_template("ventes/clients.html", rows=rows, total_due=sum(r["due"] for r in rows))


@bp.route("/clients/<int:client_id>", methods=["GET", "POST"])
@require("ventes", VIEW)
def client(client_id):
    c = query("SELECT * FROM clients WHERE id = ?", (client_id,), one=True)
    if c is None:
        abort(404)
    if request.method == "POST":
        if not can("ventes", EDIT):
            abort(403)
        name = " ".join(request.form.get("name", "").split())[:80]
        if len(name) < 2:
            flash("Écrivez le nom du client.", "error")
        else:
            execute("UPDATE clients SET name = ?, phone = ?, notes = ? WHERE id = ?",
                    (name, " ".join(request.form.get("phone", "").split())[:40], request.form.get("notes", "").strip()[:300], client_id))
            flash("Fiche client mise à jour.", "success")
        return redirect(url_for("ventes.client", client_id=client_id))
    return render_template("ventes/client.html", c=c, rows=sales_rows(client_id=client_id), bal=client_balance(client_id),
                           fmt_eggs=fmt_eggs)


@bp.route("/stock", methods=["GET", "POST"])
@require("ventes", VIEW)
def stock():
    if request.method == "POST":
        if not can("ventes", EDIT):
            abort(403)
        kind = request.form.get("kind", "inventaire")
        date = request.form.get("date") or today()
        notes = " ".join(request.form.get("notes", "").split())[:150]
        n = int(round((parse_num(request.form.get("trays"), 0) or 0) * TRAY + (parse_num(request.form.get("eggs"), 0) or 0)))
        if kind not in MOVE_KINDS or not valid_date(date) or date > today():
            flash("Choix ou date invalide.", "error")
            return redirect(url_for("ventes.stock"))
        if not any((request.form.get(k) or "").strip() for k in ("trays", "eggs")):
            flash("Écrivez le nombre d'œufs comptés (plateaux et/ou œufs). Pour un stock vide, écrivez 0.", "error")
            return redirect(url_for("ventes.stock"))
        before = egg_stock(date)
        if kind == "inventaire":
            qty = n - before
            if qty == 0:
                flash(f"Comptage conforme : {fmt_eggs(n)}, aucun écart. 👍", "success")
                log_activity("Inventaire des œufs conforme", f"{date_fr(date)} : {n} œufs")
                notify("", None, f"✅ Inventaire des œufs du {date_fr(date)} : {fmt_eggs(n)} comptés, conforme au logiciel.")
                return redirect(url_for("ventes.stock"))
        else:
            if n <= 0:
                flash("Indiquez le nombre d'œufs.", "error")
                return redirect(url_for("ventes.stock"))
            qty = n if kind == "ajout" else -n
        execute("INSERT INTO egg_moves (date, kind, quantity, notes, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (date, kind, qty, notes, g.user["id"], now_utc()))
        txt = (f"📋 Inventaire des œufs du {date_fr(date)} : logiciel {fmt_eggs(before)}, compté {fmt_eggs(n)} → écart "
               f"{'+' if qty > 0 else ''}{qty} œufs" if kind == "inventaire" else
               f"🥚 Stock d'œufs : {MOVE_KINDS[kind].lower()} de {fmt_eggs(abs(qty))} le {date_fr(date)}")
        notify("", None, txt + (f" — explication : « {notes} »" if notes else " — ⚠️ sans explication"))
        log_activity("Stock d'œufs corrigé", txt)
        flash("Enregistré. " + ("Écart à expliquer dans « Écarts & contrôles »." if not notes and kind != "ajout" else "Vos associés sont prévenus."),
              "success")
        return redirect(url_for("ventes.stock"))
    ledger = egg_ledger()
    return render_template("ventes/stock.html", stock=egg_stock(), ledger=list(reversed(ledger))[:150], kinds=MOVE_KINDS,
                           chart=stock_series(30), today=today(), fmt_eggs=fmt_eggs, tray=TRAY)


@bp.route("/stock/<int:move_id>/annuler", methods=["POST"])
@require("ventes", EDIT)
def move_delete(move_id):
    m = query("SELECT * FROM egg_moves WHERE id = ?", (move_id,), one=True)
    if m is None:
        abort(404)
    if not can_correct("ventes", m):
        abort(403)
    execute("DELETE FROM egg_moves WHERE id = ?", (move_id,))
    notify("", None, f"↩️ Correction du stock d'œufs du {date_fr(m['date'])} annulée ({m['quantity']} œufs).")
    flash("Correction annulée.", "success")
    return redirect(url_for("ventes.stock"))


__all__ = ["bp", "egg_stock", "lot_revenue", "total_due", "egg_move_gaps", "MANAGE"]
