"""Immobilisations : bâtiments, cages, abreuvoirs, groupe électrogène, véhicule… les biens qui servent plusieurs années.

On ne compte pas leur prix d'un coup comme une dépense : on compte leur USURE (amortissement), un peu chaque jour,
pendant toute leur durée de vie.
Exemple : des cages à 3 650 000 Ar qui durent 10 ans s'usent de 3 650 000 ÷ (10 × 365) = 1 000 Ar par jour.
Cette usure est partagée entre les lots présents (ou mise sur un seul lot si le bien ne sert qu'à lui)
et compte dans le coût d'un œuf.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .costs import asset_state, by_month
from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, require
from .stock import accounts, parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("immobilisations", __name__, url_prefix="/immobilisations")

CATEGORIES = {"Bâtiment": 20, "Cages / batteries": 10, "Abreuvoirs / mangeoires": 5, "Groupe électrogène": 5,
              "Pompe / citerne / forage": 8, "Véhicule / moto": 5, "Matériel de provenderie": 8, "Clôture": 10,
              "Petit matériel": 3, "Autre": 5}


def all_assets():
    rows = query("""SELECT a.*, l.name AS lot, ac.name AS account FROM assets a LEFT JOIN lots l ON l.id = a.lot_id
                    LEFT JOIN accounts ac ON ac.id = a.account_id ORDER BY a.status = 'actif' DESC, a.purchase_date DESC""")
    return [dict(r, st=asset_state(r)) for r in rows]


@bp.route("/")
@require("immobilisations", VIEW)
def index():
    items = all_assets()
    active = [a for a in items if a["status"] == "actif"]
    from .expenses import _mlabel, _months

    keys = _months(12)
    per = by_month(keys)
    first = next((i for i, k in enumerate(keys) if per[k]["usure"]), len(keys) - 1)
    keys = keys[first:] or keys[-1:]
    chart = {"labels": [_mlabel(k) for k in keys], "bars": [round(per[k]["usure"]) for k in keys], "line": [None] * len(keys),
             "unit": "Ar", "barLabel": "Usure du matériel", "barColor": "#b07d10"}
    kpi = {"cost": sum(a["cost"] for a in active), "value": sum(a["st"]["value"] for a in active),
           "monthly": sum(a["st"]["monthly"] for a in active if not a["st"]["finished"]), "count": len(active)}
    lots = query("SELECT id, name FROM lots WHERE status = 'actif' ORDER BY arrival_date")
    return render_template("immobilisations/index.html", items=items, chart=chart, has_chart=any(per[k]["usure"] for k in keys),
                           kpi=kpi, cats=CATEGORIES, lots=lots, accounts=accounts(), today=today())


def _form():
    f = request.form
    v = {"name": " ".join(f.get("name", "").split())[:80], "category": " ".join(f.get("category", "").split())[:60] or "Autre",
         "purchase_date": f.get("purchase_date") or today(), "cost": parse_num(f.get("cost")), "life_years": parse_num(f.get("life_years")),
         "residual": parse_num(f.get("residual"), 0) or 0, "lot_id": f.get("lot_id") or None,
         "account_id": f.get("account_id") or None, "notes": f.get("notes", "").strip()[:300]}
    errors = []
    if len(v["name"]) < 2:
        errors.append("Écrivez le nom du bien (ex. Cages bâtiment 2).")
    if not valid_date(v["purchase_date"]) or v["purchase_date"] > today():
        errors.append("Date d'achat invalide.")
    if v["cost"] is None or v["cost"] <= 0:
        errors.append("Écrivez le prix d'achat.")
    if v["life_years"] is None or v["life_years"] <= 0 or v["life_years"] > 50:
        errors.append("Écrivez combien d'années il va servir (entre 1 et 50).")
    if v["cost"] and v["residual"] and (v["residual"] < 0 or v["residual"] >= v["cost"]):
        errors.append("La valeur à la fin doit être plus petite que le prix d'achat.")
    if v["account_id"] and not query("SELECT 1 FROM accounts WHERE id = ?", (v["account_id"],), one=True):
        v["account_id"] = None
    return v, errors


def _cash(conn, asset_id, v, name):
    conn.execute("DELETE FROM cash_movements WHERE ref_type = 'asset' AND ref_id = ? AND kind = 'immobilisation'", (asset_id,))
    if v["account_id"]:
        conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                        VALUES (?, ?, ?, 'immobilisation', ?, 'asset', ?, ?, ?)""",
                     (v["purchase_date"], v["account_id"], -v["cost"], f"Achat : {name}", asset_id, g.user["id"], now_utc()))


@bp.route("/nouveau", methods=["POST"])
@require("immobilisations", EDIT)
def new():
    v, errors = _form()
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("immobilisations.index") + "#nouveau")
    with transaction() as conn:
        cur = conn.execute("""INSERT INTO assets (name, category, purchase_date, cost, life_years, residual, lot_id, account_id, notes,
                              created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (v["name"], v["category"], v["purchase_date"], v["cost"], v["life_years"], v["residual"], v["lot_id"],
                            v["account_id"], v["notes"], g.user["id"], now_utc()))
        asset_id = cur.lastrowid
        _cash(conn, asset_id, v, v["name"])
    st = asset_state(query("SELECT * FROM assets WHERE id = ?", (asset_id,), one=True))
    log_activity("Immobilisation ajoutée", f"{v['name']} {fmt_money(v['cost'])}")
    notify("asset", asset_id, f"🏗️ Nouveau bien : {v['name']} · {fmt_money(v['cost'])} · {v['life_years']:g} an(s) → usure "
           f"{fmt_money(st['monthly'])} par mois." + (" Payé avec la caisse." if v["account_id"] else ""))
    flash(f"Bien enregistré. Son usure ({fmt_money(st['daily'])} par jour, {fmt_money(st['monthly'])} par mois) compte maintenant dans le coût des œufs.",
          "success")
    return redirect(url_for("immobilisations.asset", asset_id=asset_id))


def _get(asset_id):
    row = query("""SELECT a.*, l.name AS lot, ac.name AS account FROM assets a LEFT JOIN lots l ON l.id = a.lot_id
                   LEFT JOIN accounts ac ON ac.id = a.account_id WHERE a.id = ?""", (asset_id,), one=True)
    if row is None:
        abort(404)
    return row


@bp.route("/<int:asset_id>")
@require("immobilisations", VIEW)
def asset(asset_id):
    a = _get(asset_id)
    st = asset_state(a)
    from datetime import date as Date

    start = Date.fromisoformat(a["purchase_date"])
    years, used = [], 0.0
    total = a["cost"] - (a["residual"] or 0)
    for k in range(int(a["life_years"] + 0.999)):
        part = min(st["yearly"], total - used)
        used += part
        years.append({"n": k + 1, "year": start.year + k, "part": part, "left": a["cost"] - used})
    lots = query("SELECT id, name FROM lots ORDER BY status = 'actif' DESC, arrival_date DESC")
    return render_template("immobilisations/asset.html", a=a, st=st, years=years, cats=CATEGORIES, lots=lots, accounts=accounts(), today=today())


@bp.route("/<int:asset_id>/modifier", methods=["POST"])
@require("immobilisations", MANAGE)
def edit(asset_id):
    a = _get(asset_id)
    v, errors = _form()
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("immobilisations.asset", asset_id=asset_id))
    with transaction() as conn:
        conn.execute("""UPDATE assets SET name = ?, category = ?, purchase_date = ?, cost = ?, life_years = ?, residual = ?, lot_id = ?,
                        account_id = ?, notes = ? WHERE id = ?""",
                     (v["name"], v["category"], v["purchase_date"], v["cost"], v["life_years"], v["residual"], v["lot_id"],
                      v["account_id"], v["notes"], asset_id))
        _cash(conn, asset_id, v, v["name"])
    notify("asset", asset_id, f"✏️ Bien « {v['name']} » corrigé : {fmt_money(a['cost'])} / {a['life_years']:g} an(s) → "
           f"{fmt_money(v['cost'])} / {v['life_years']:g} an(s).")
    flash("Bien corrigé : son usure est recalculée.", "success")
    return redirect(url_for("immobilisations.asset", asset_id=asset_id))


@bp.route("/<int:asset_id>/sortie", methods=["POST"])
@require("immobilisations", MANAGE)
def retire(asset_id):
    a = _get(asset_id)
    if a["status"] != "actif":
        with transaction() as conn:
            conn.execute("UPDATE assets SET status = 'actif', end_date = '', sale_price = 0 WHERE id = ?", (asset_id,))
            conn.execute("DELETE FROM cash_movements WHERE ref_type = 'asset' AND ref_id = ? AND kind = 'vente_bien'", (asset_id,))
        flash("Le bien est de nouveau en service.", "success")
        return redirect(url_for("immobilisations.asset", asset_id=asset_id))
    end = request.form.get("end_date") or today()
    price = parse_num(request.form.get("sale_price"), 0) or 0
    account = query("SELECT * FROM accounts WHERE id = ?", (request.form.get("account_id") or 0,), one=True)
    reason = " ".join(request.form.get("reason", "").split())[:150]
    if not valid_date(end) or end < a["purchase_date"] or end > today():
        flash("Date invalide.", "error")
        return redirect(url_for("immobilisations.asset", asset_id=asset_id))
    if price > 0 and account is None:
        flash("Choisissez la caisse où entre l'argent de la vente.", "error")
        return redirect(url_for("immobilisations.asset", asset_id=asset_id))
    with transaction() as conn:
        conn.execute("UPDATE assets SET status = 'sorti', end_date = ?, sale_price = ?, notes = CASE WHEN ? != '' THEN ? ELSE notes END WHERE id = ?",
                     (end, price, reason, reason, asset_id))
        if price > 0:
            conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                            VALUES (?, ?, ?, 'vente_bien', ?, 'asset', ?, ?, ?)""",
                         (end, account["id"], price, f"Vente : {a['name']}", asset_id, g.user["id"], now_utc()))
    st = asset_state(_get(asset_id), end)
    notify("asset", asset_id, f"📤 Bien « {a['name']} » sorti le {date_fr(end)}" + (f", vendu {fmt_money(price)}" if price else "")
           + f" (valeur restante {fmt_money(st['value'])})." + (f" « {reason} »" if reason else ""))
    flash("Sortie enregistrée : l'usure s'arrête à cette date.", "success")
    return redirect(url_for("immobilisations.asset", asset_id=asset_id))


@bp.route("/<int:asset_id>/supprimer", methods=["POST"])
@require("immobilisations", MANAGE)
def delete(asset_id):
    a = _get(asset_id)
    with transaction() as conn:
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'asset' AND ref_id = ?", (asset_id,))
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    log_activity("Immobilisation supprimée", a["name"])
    notify("", None, f"↩️ Bien « {a['name']} » supprimé (erreur de saisie).")
    flash("Bien supprimé.", "success")
    return redirect(url_for("immobilisations.index"))


__all__ = ["bp", "all_assets"]
