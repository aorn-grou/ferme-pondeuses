"""Caisse & propriétaires : où est l'argent, qui a mis quoi, qui a retiré quoi.

Deux sortes de comptes :
- Les CAISSES (caisse de la ferme, mobile money, banque…) : l'argent réellement disponible.
  Leur solde = toutes les entrées − toutes les sorties.
- Les PROPRIÉTAIRES : leur « compte » garde la trace de ce qu'ils ont mis dans la ferme et de ce qu'ils ont retiré.
  Un APPORT (le propriétaire met 500 000 Ar dans la caisse) : la caisse monte de 500 000, et le propriétaire
  « a mis » 500 000 de plus. Un RETRAIT : la caisse baisse, le propriétaire « a mis » moins.
  Quand un propriétaire paie un achat de sa poche (« Payé avec : Propriétaire 1 »), cela compte aussi comme mis.
  Dans la base, ce qu'il a mis = − le solde de son compte.

Le comptage de la caisse : on compte les billets, le logiciel compare avec ce qu'il attend ;
la différence devient un écart (« Écarts & contrôles ») à expliquer.
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, require
from .stock import parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("caisse", __name__, url_prefix="/caisse")

# Mouvements créés ici (les autres viennent des ventes, achats, dépenses… et se corrigent là-bas)
MANUAL = {"apport", "retrait", "transfert", "entree", "ecart_caisse", "solde_depart"}
KIND_LABELS = {
    "vente": "Vente", "encaissement": "Encaissement d'un crédit", "achat": "Achat de matières", "reglement": "Paiement fournisseur",
    "reglement_fournisseur": "Paiement fournisseur", "achat_poussins": "Achat de poussins", "sanitaire": "Soin",
    "depense": "Dépense", "salaire": "Salaire / avance", "immobilisation": "Achat de matériel", "vente_bien": "Vente de matériel",
    "apport": "Apport du propriétaire", "retrait": "Retrait du propriétaire", "transfert": "Transfert entre comptes",
    "entree": "Autre entrée d'argent", "ecart_caisse": "Écart de caisse (comptage)", "solde_depart": "Argent au départ",
}
REF_LINKS = {"sale": ("ventes.sale", "sale_id"), "sale_payment": None, "purchase": ("matieres.purchase", "purchase_id"),
             "lot": ("lots.lot", "lot_id"), "expense": ("depenses.item", "exp_id"), "staff_payment": None,
             "asset": ("immobilisations.asset", "asset_id")}


def is_owner(account):
    return account["kind"] == "proprietaire"


def balances():
    rows = query("""SELECT a.*, COALESCE(SUM(c.amount), 0) AS balance, COUNT(c.id) AS moves,
                           COALESCE(SUM(CASE WHEN c.amount > 0 THEN c.amount ELSE 0 END), 0) AS ins,
                           COALESCE(SUM(CASE WHEN c.amount < 0 THEN -c.amount ELSE 0 END), 0) AS outs
                    FROM accounts a LEFT JOIN cash_movements c ON c.account_id = a.id
                    WHERE a.active = 1 OR a.id IN (SELECT DISTINCT account_id FROM cash_movements)
                    GROUP BY a.id ORDER BY a.sort, a.name""")
    return [dict(r) for r in rows]


def balance(account_id, until=None):
    sql, params = "SELECT COALESCE(SUM(amount), 0) AS b FROM cash_movements WHERE account_id = ?", [account_id]
    if until:
        sql += " AND date <= ?"
        params.append(until)
    return query(sql, params, one=True)["b"]


def farm_result():
    """Résultat de la ferme depuis le début = ventes − tout ce qui a été consommé ou dépensé
    (poussins, provende mangée, soins, dépenses, salaires, usure du matériel)."""
    from .costs import allocate, lot_costs

    sales = query("SELECT COALESCE(SUM(total), 0) AS t FROM sales", one=True)["t"]
    costs = 0.0
    for lot in query("SELECT * FROM lots"):
        costs += lot_costs(lot)["total"]
    _shares, rest = allocate()
    costs += sum(rest.values())
    return {"sales": sales, "costs": costs, "result": sales - costs}


def owners_view():
    owners = [b for b in balances() if is_owner(b)]
    if not owners:
        return {"owners": [], "result": None}
    res = farm_result()
    total_share = sum(o["share"] or 0 for o in owners)
    for o in owners:
        share = (o["share"] or 0) if total_share else 100 / len(owners)
        o["pct"] = share if total_share else 100 / len(owners)
        mv = {r["kind"]: r["t"] for r in query("SELECT kind, SUM(amount) AS t FROM cash_movements WHERE account_id = ? GROUP BY kind",
                                               (o["id"],))}
        o["apports"] = -(mv.get("apport", 0) or 0)
        o["retraits"] = mv.get("retrait", 0) or 0
        o["paid_for_farm"] = -sum(v for k, v in mv.items() if k not in ("apport", "retrait") and v < 0)
        o["received"] = sum(v for k, v in mv.items() if k not in ("apport", "retrait") and v > 0)
        o["put"] = -o["balance"]  # ce qu'il a mis (net)
        o["result_part"] = res["result"] * o["pct"] / 100
        o["worth"] = o["put"] + o["result_part"]
    return {"owners": owners, "result": res, "shares_set": bool(total_share)}


def treasury_series(days=90):
    end = Date.fromisoformat(today())
    start = end - timedelta(days=days - 1)
    ids = [a["id"] for a in query("SELECT id FROM accounts WHERE kind != 'proprietaire'")]
    if not ids:
        return None
    ph = ",".join("?" * len(ids))
    before = query(f"SELECT COALESCE(SUM(amount), 0) AS b FROM cash_movements WHERE date < ? AND account_id IN ({ph})",
                   (start.isoformat(), *ids), one=True)["b"]
    per = {r["date"]: r["t"] for r in query(f"""SELECT date, SUM(amount) AS t FROM cash_movements WHERE date >= ? AND date <= ?
                                                 AND account_id IN ({ph}) GROUP BY date""", (start.isoformat(), end.isoformat(), *ids))}
    first = query(f"SELECT MIN(date) AS d FROM cash_movements WHERE account_id IN ({ph})", ids, one=True)["d"]
    if not first:
        return None
    labels, values, run = [], [], before
    for k in range(days):
        d = start + timedelta(days=k)
        run += per.get(d.isoformat(), 0)
        if d.isoformat() < first:
            continue
        labels.append(f"{d:%d/%m}")
        values.append(round(run))
    return {"labels": labels, "values": values, "unit": "Ar", "color": "#1f6fa8", "allowNegative": True}


def month_flows(n=6):
    end = Date.fromisoformat(today()).replace(day=1)
    keys, y, m = [], end.year, end.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    rows = {r["k"]: r for r in query("""SELECT substr(c.date, 1, 7) AS k,
            SUM(CASE WHEN c.amount > 0 AND c.kind NOT IN ('apport', 'transfert') THEN c.amount ELSE 0 END) AS ins,
            SUM(CASE WHEN c.amount < 0 AND c.kind NOT IN ('retrait', 'transfert') THEN -c.amount ELSE 0 END) AS outs,
            SUM(CASE WHEN c.kind = 'apport' AND c.amount > 0 THEN c.amount ELSE 0 END) AS apports,
            SUM(CASE WHEN c.kind = 'retrait' AND c.amount < 0 THEN -c.amount ELSE 0 END) AS retraits
            FROM cash_movements c JOIN accounts a ON a.id = c.account_id WHERE a.kind != 'proprietaire' AND c.date >= ?
            GROUP BY k""", (keys[0] + "-01",))}
    from .expenses import _mlabel

    return [{"key": k, "label": _mlabel(k), "ins": (rows[k]["ins"] if k in rows else 0) or 0,
             "outs": (rows[k]["outs"] if k in rows else 0) or 0, "apports": (rows[k]["apports"] if k in rows else 0) or 0,
             "retraits": (rows[k]["retraits"] if k in rows else 0) or 0} for k in keys]


def alerts():
    out = []
    for b in balances():
        if not is_owner(b) and b["balance"] < -0.5:
            out.append({"level": "warning", "title": f"💰 La caisse « {b['name']} » est en négatif ({fmt_money(b['balance'])})",
                        "text": "Il est sorti plus d'argent qu'il n'en est entré : une entrée a été oubliée (apport, vente…) ou une sortie "
                                "a été mise sur la mauvaise caisse. Faites un comptage de caisse.",
                        "url": url_for("caisse.account", account_id=b["id"])})
    return out


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@bp.route("/")
@require("caisse", VIEW)
def index():
    rows = balances()
    cash = [b for b in rows if not is_owner(b)]
    own = owners_view() if can("caisse", VIEW) else {"owners": []}
    recent = query("""SELECT c.*, a.name AS account, a.kind AS account_kind, COALESCE(NULLIF(u.full_name, ''), u.username) AS username
                      FROM cash_movements c LEFT JOIN accounts a ON a.id = c.account_id LEFT JOIN users u ON u.id = c.created_by
                      ORDER BY c.date DESC, c.id DESC LIMIT 25""")
    return render_template("caisse/index.html", cash=cash, total=sum(b["balance"] for b in cash), own=own,
                           owners=[b for b in rows if is_owner(b) and b["active"]], accounts=[b for b in cash if b["active"]],
                           chart=treasury_series(), flows=month_flows(), recent=recent, kinds=KIND_LABELS, manual=MANUAL,
                           today=today())


@bp.route("/compte/<int:account_id>")
@require("caisse", VIEW)
def account(account_id):
    acc = query("SELECT * FROM accounts WHERE id = ?", (account_id,), one=True)
    if acc is None:
        abort(404)
    rows = query("""SELECT c.*, COALESCE(NULLIF(u.full_name, ''), u.username) AS username FROM cash_movements c
                    LEFT JOIN users u ON u.id = c.created_by WHERE c.account_id = ? ORDER BY c.date, c.id""", (account_id,))
    run, items = 0.0, []
    for r in rows:
        run += r["amount"]
        link = REF_LINKS.get(r["ref_type"] or "")
        items.append(dict(r, balance=run, link=url_for(link[0], **{link[1]: r["ref_id"]}) if link and r["ref_id"] else None))
    items.reverse()
    return render_template("caisse/account.html", acc=acc, items=items, total=run, kinds=KIND_LABELS, manual=MANUAL,
                           owner=is_owner(acc), today=today())


def _acc(value, owner=None):
    row = query("SELECT * FROM accounts WHERE id = ?", (value or 0,), one=True)
    if row is None:
        return None
    if owner is True and not is_owner(row):
        return None
    if owner is False and is_owner(row):
        return None
    return row


def _move(conn, date, account_id, amount, kind, label, notes=""):
    cur = conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, notes, created_by, created_at)
                          VALUES (?, ?, ?, ?, ?, 'caisse', ?, ?, ?)""",
                       (date, account_id, amount, kind, label, notes, g.user["id"], now_utc()))
    return cur.lastrowid


@bp.route("/operation", methods=["POST"])
@require("caisse", EDIT)
def operation():
    f = request.form
    op = f.get("op", "")
    date = f.get("date") or today()
    amount = parse_num(f.get("amount"))
    notes = " ".join(f.get("notes", "").split())[:200]
    back = redirect(url_for("caisse.index") + "#operations")
    if not valid_date(date) or date > today():
        flash("Date invalide (pas dans le futur).", "error")
        return back
    if op != "comptage" and (amount is None or amount <= 0):
        flash("Écrivez un montant plus grand que 0.", "error")
        return back
    if op in ("apport", "retrait"):
        owner, box = _acc(f.get("owner_id"), True), _acc(f.get("account_id"), False)
        if not owner or not box:
            flash("Choisissez le propriétaire et la caisse.", "error")
            return back
        sign = 1 if op == "apport" else -1
        what = "Apport" if op == "apport" else "Retrait"
        with transaction() as conn:
            _move(conn, date, box["id"], sign * amount, op, f"{what} de {owner['name']}", notes)
            _move(conn, date, owner["id"], -sign * amount, op, f"{what} {'dans' if sign > 0 else 'de'} « {box['name']} »", notes)
        msg = (f"💵 Apport : {owner['name']} a mis {fmt_money(amount)} dans « {box['name']} »" if op == "apport"
               else f"💸 Retrait : {owner['name']} a pris {fmt_money(amount)} dans « {box['name']} »")
        notify("account", box["id"], msg + f" le {date_fr(date)}." + (f" ({notes})" if notes else ""))
        log_activity(what, f"{owner['name']} {fmt_money(amount)} ({box['name']})")
        flash(f"{what} enregistré. Solde de « {box['name']} » : {fmt_money(balance(box['id']))}.", "success")
    elif op == "transfert":
        a, b = _acc(f.get("from_id"), False), _acc(f.get("account_id"), False)
        if not a or not b or a["id"] == b["id"]:
            flash("Choisissez deux caisses différentes.", "error")
            return back
        with transaction() as conn:
            _move(conn, date, a["id"], -amount, "transfert", f"Vers « {b['name']} »", notes)
            _move(conn, date, b["id"], amount, "transfert", f"Depuis « {a['name']} »", notes)
        notify("account", a["id"], f"🔁 Transfert de {fmt_money(amount)} : « {a['name']} » → « {b['name']} » le {date_fr(date)}.")
        log_activity("Transfert", f"{a['name']} → {b['name']} {fmt_money(amount)}")
        flash("Transfert enregistré.", "success")
    elif op == "entree":
        box = _acc(f.get("account_id"), False)
        label = " ".join(f.get("label", "").split())[:100]
        if not box or not label:
            flash("Choisissez la caisse et écrivez d'où vient l'argent (ex. subvention, prêt…).", "error")
            return back
        with transaction() as conn:
            _move(conn, date, box["id"], amount, "entree", label, notes)
        notify("account", box["id"], f"💵 Entrée d'argent : {label} · {fmt_money(amount)} dans « {box['name']} » le {date_fr(date)}.")
        log_activity("Entrée d'argent", f"{label} {fmt_money(amount)}")
        flash("Entrée d'argent enregistrée.", "success")
    elif op == "comptage":
        box = _acc(f.get("account_id"), False)
        counted = parse_num(f.get("counted"))
        if not box or counted is None or counted < 0:
            flash("Choisissez la caisse et écrivez l'argent compté.", "error")
            return back
        expected = balance(box["id"], date)
        diff = counted - expected
        if abs(diff) < 0.5:
            flash(f"Parfait ✅ : la caisse « {box['name']} » contient bien {fmt_money(expected)}, aucun écart.", "success")
            notify("account", box["id"], f"✅ Comptage de « {box['name']} » le {date_fr(date)} : {fmt_money(counted)}, aucun écart.")
            return back
        with transaction() as conn:
            _move(conn, date, box["id"], diff, "ecart_caisse", f"Comptage : attendu {fmt_money(expected)}, compté {fmt_money(counted)}", notes)
        notify("account", box["id"], f"{'⚠️' if diff < 0 else 'ℹ️'} Comptage de « {box['name']} » le {date_fr(date)} : attendu {fmt_money(expected)}, "
               f"compté {fmt_money(counted)} → écart {'+' if diff > 0 else ''}{fmt_money(diff)}." + (f" « {notes} »" if notes else " Sans explication."))
        log_activity("Comptage de caisse", f"{box['name']} écart {fmt_money(diff)}")
        flash(f"Écart de {'+' if diff > 0 else ''}{fmt_money(diff)} enregistré : la caisse est corrigée. "
              + ("Il apparaît dans Écarts & contrôles." if notes else "Expliquez-le dans Écarts & contrôles."), "success" if notes else "error")
    elif op == "depart":
        box = _acc(f.get("account_id"))
        if not box:
            flash("Choisissez le compte.", "error")
            return back
        with transaction() as conn:
            _move(conn, date, box["id"], amount if not is_owner(box) else -amount, "solde_depart",
                  "Argent au départ (avant le logiciel)", notes)
        flash("Argent de départ enregistré.", "success")
    else:
        abort(400)
    return back


@bp.route("/mouvement/<int:move_id>/supprimer", methods=["POST"])
@require("caisse", MANAGE)
def move_delete(move_id):
    mv = query("SELECT * FROM cash_movements WHERE id = ?", (move_id,), one=True)
    if mv is None:
        abort(404)
    if mv["kind"] not in MANUAL:
        flash("Ce mouvement vient d'une autre page (vente, achat, dépense…) : corrigez-le là-bas.", "error")
        return redirect(request.referrer or url_for("caisse.index"))
    with transaction() as conn:
        # un apport, un retrait ou un transfert a deux côtés : on supprime les deux
        if mv["kind"] in ("apport", "retrait", "transfert"):
            twin = conn.execute("""SELECT id FROM cash_movements WHERE kind = ? AND date = ? AND amount = ? AND created_at = ?
                                   AND id != ? ORDER BY ABS(id - ?) LIMIT 1""",
                                (mv["kind"], mv["date"], -mv["amount"], mv["created_at"], move_id, move_id)).fetchone()
            if twin:
                conn.execute("DELETE FROM cash_movements WHERE id = ?", (twin["id"],))
        conn.execute("DELETE FROM cash_movements WHERE id = ?", (move_id,))
    log_activity("Mouvement de caisse supprimé", f"{KIND_LABELS.get(mv['kind'], mv['kind'])} {fmt_money(mv['amount'])}")
    notify("account", mv["account_id"], f"↩️ Mouvement supprimé : {KIND_LABELS.get(mv['kind'], mv['kind'])} {fmt_money(mv['amount'])} du {date_fr(mv['date'])}.")
    flash("Mouvement supprimé.", "success")
    return redirect(request.referrer or url_for("caisse.index"))


@bp.route("/parts", methods=["POST"])
@require("caisse", MANAGE)
def shares():
    total = 0
    vals = {}
    for o in query("SELECT * FROM accounts WHERE kind = 'proprietaire'"):
        v = parse_num(request.form.get(f"share_{o['id']}"), 0) or 0
        if v < 0 or v > 100:
            flash("Une part doit être entre 0 et 100 %.", "error")
            return redirect(url_for("caisse.index") + "#proprietaires")
        vals[o["id"]] = v
        total += v
    if vals and abs(total - 100) > 0.01:
        flash(f"Les parts doivent faire 100 % au total (vous avez écrit {total:g} %).", "error")
        return redirect(url_for("caisse.index") + "#proprietaires")
    for k, v in vals.items():
        execute("UPDATE accounts SET share = ? WHERE id = ?", (v, k))
    log_activity("Parts des propriétaires", ", ".join(f"{v:g} %" for v in vals.values()))
    flash("Parts enregistrées.", "success")
    return redirect(url_for("caisse.index") + "#proprietaires")


__all__ = ["bp", "alerts", "balances", "farm_result", "owners_view", "KIND_LABELS"]
