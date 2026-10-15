"""Dépenses : électricité, eau, transport, réparations, litière… tout ce qui n'est ni un achat de matières,
ni des poussins, ni de la provende.

- Une dépense est « payée » (l'argent sort d'une caisse) ou « à payer » (une dette, suivie en rouge).
- Elle est pour un lot précis, ou commune : une dépense commune est partagée entre les lots présents
  ce jour-là selon leur nombre de poules (voir app/costs.py). Elle compte dans le coût d'un œuf de chaque lot.
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, can_correct, require
from .stock import accounts, parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("depenses", __name__, url_prefix="/depenses")

CATEGORIES = ["Électricité", "Eau", "Transport", "Carburant", "Réparation / entretien", "Litière / copeaux",
              "Plateaux / emballages", "Désinfection / hygiène", "Petit matériel", "Loyer / terrain",
              "Téléphone / internet", "Impôts & taxes", "Gardiennage", "Main-d'œuvre occasionnelle", "Divers"]
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _mlabel(key):
    return f"{MONTHS[int(key[5:7]) - 1]} {key[2:4]}"


def _months(n, end=None):
    end = Date.fromisoformat(end or today()).replace(day=1)
    keys, y, m = [], end.year, end.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def categories():
    used = [r["category"] for r in query("SELECT category, COUNT(*) AS n FROM expenses GROUP BY category ORDER BY n DESC")]
    return list(dict.fromkeys(used + CATEGORIES))


def rows(where="1 = 1", params=()):
    return query(f"""SELECT e.*, l.name AS lot, a.name AS account, COALESCE(NULLIF(u.full_name, ''), u.username) AS username
                     FROM expenses e LEFT JOIN lots l ON l.id = e.lot_id LEFT JOIN accounts a ON a.id = e.account_id
                     LEFT JOIN users u ON u.id = e.created_by WHERE {where} ORDER BY e.date DESC, e.id DESC""", params)


def unpaid():
    return query("""SELECT e.*, l.name AS lot FROM expenses e LEFT JOIN lots l ON l.id = e.lot_id
                    WHERE e.status = 'a_payer' ORDER BY e.date""")


def total_unpaid():
    return query("SELECT COALESCE(SUM(amount), 0) AS t FROM expenses WHERE status = 'a_payer'", one=True)["t"]


def alerts():
    out = []
    lim = (Date.fromisoformat(today()) - timedelta(days=30)).isoformat()
    old = [e for e in unpaid() if e["date"] <= lim]
    if old:
        out.append({"level": "warning", "title": f"🧾 {len(old)} dépense(s) pas encore payée(s) depuis plus de 30 jours",
                    "text": f"Total {fmt_money(sum(e['amount'] for e in old))}. Payez-les ou corrigez-les dans Dépenses.",
                    "url": url_for("depenses.index", voir="a_payer")})
    return out


def _cash(conn, exp_id, date, account_id, amount, label):
    conn.execute("DELETE FROM cash_movements WHERE ref_type = 'expense' AND ref_id = ?", (exp_id,))
    if account_id and amount > 0:
        conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                        VALUES (?, ?, ?, 'depense', ?, 'expense', ?, ?, ?)""",
                     (date, account_id, -amount, label, exp_id, g.user["id"], now_utc()))


# ---------------------------------------------------------------------------
# Page principale
# ---------------------------------------------------------------------------
@bp.route("/")
@require("depenses", VIEW)
def index():
    show = request.args.get("voir", "")
    period = request.args.get("mois", "")
    cat = request.args.get("categorie", "")
    lot_id = request.args.get("lot", type=int)
    where, params = ["1 = 1"], []
    if show == "a_payer":
        where.append("e.status = 'a_payer'")
    elif show == "tout":
        pass
    if period and len(period) == 7:
        where.append("substr(e.date, 1, 7) = ?")
        params.append(period)
    elif not show:
        where.append("e.date >= ?")
        params.append((Date.fromisoformat(today()) - timedelta(days=90)).isoformat())
    if cat:
        where.append("e.category = ?")
        params.append(cat)
    if lot_id:
        where.append("e.lot_id = ?")
        params.append(lot_id)
    items = rows(" AND ".join(where), tuple(params))

    keys = _months(12)
    per = {r["k"]: r["t"] for r in query("SELECT substr(date, 1, 7) AS k, SUM(amount) AS t FROM expenses WHERE date >= ? GROUP BY k",
                                         (keys[0] + "-01",))}
    first = next((i for i, k in enumerate(keys) if per.get(k)), len(keys) - 1)
    keys = keys[first:] or keys[-1:]
    cur = today()[:7]
    chart = {"labels": [_mlabel(k) + ("*" if k == cur else "") for k in keys], "bars": [round(per.get(k, 0)) for k in keys],
             "line": [None] * len(keys), "unit": "Ar", "barLabel": "Dépenses", "barColor": "#c0352b", "now": _mlabel(cur) + "*",
             "nowLabel": "Ce mois (pas fini)"}
    since = (Date.fromisoformat(today()) - timedelta(days=90)).isoformat()
    by_cat = query("SELECT category AS name, SUM(amount) AS value, COUNT(*) AS count FROM expenses WHERE date >= ? "
                   "GROUP BY category ORDER BY value DESC", (since,))
    total_cat = sum(c["value"] for c in by_cat) or 1
    by_cat = [dict(c, pct=c["value"] / total_cat * 100) for c in by_cat]
    prev = _months(2)[0]
    full = [k for k in _months(7)[:-1]]
    kpi = {"month": per.get(cur, 0), "prev": per.get(prev, 0), "unpaid": total_unpaid(),
           "avg": sum(per.get(k, 0) for k in full) / 6, "common": query(
               "SELECT COALESCE(SUM(amount), 0) AS t FROM expenses WHERE lot_id IS NULL AND substr(date, 1, 7) = ?", (cur,), one=True)["t"]}
    lots = query("SELECT id, name, status FROM lots ORDER BY status = 'actif' DESC, arrival_date DESC")
    return render_template("depenses/index.html", items=items, chart=chart, by_cat=by_cat, kpi=kpi, lots=lots,
                           cats=categories(), accounts=accounts(), today=today(), show=show, period=period, cat=cat,
                           lot_id=lot_id, months=list(reversed(_months(12))), mlabel=_mlabel, has_chart=any(per.values()))


def _form():
    f = request.form
    v = {"date": f.get("date") or today(), "category": " ".join(f.get("category", "").split())[:60] or "Divers",
         "label": " ".join(f.get("label", "").split())[:120], "amount": parse_num(f.get("amount")),
         "lot_id": f.get("lot_id") or None, "beneficiary": " ".join(f.get("beneficiary", "").split())[:80],
         "status": "a_payer" if f.get("unpaid") else "paye", "account_id": f.get("account_id") or None,
         "notes": f.get("notes", "").strip()[:300]}
    errors = []
    if not valid_date(v["date"]) or v["date"] > today():
        errors.append("Date invalide (une dépense ne peut pas être dans le futur).")
    if v["amount"] is None or v["amount"] <= 0:
        errors.append("Écrivez le montant (un nombre plus grand que 0).")
    if v["lot_id"] and not query("SELECT 1 FROM lots WHERE id = ?", (v["lot_id"],), one=True):
        v["lot_id"] = None
    if v["status"] == "paye":
        if not v["account_id"] or not query("SELECT 1 FROM accounts WHERE id = ?", (v["account_id"],), one=True):
            errors.append("Choisissez d'où vient l'argent (caisse…), ou cochez « pas encore payé ».")
    else:
        v["account_id"] = None
    return v, errors


@bp.route("/nouvelle", methods=["POST"])
@require("depenses", EDIT)
def new():
    v, errors = _form()
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("depenses.index") + "#nouvelle")
    with transaction() as conn:
        cur = conn.execute("""INSERT INTO expenses (date, category, label, amount, lot_id, beneficiary, status, paid_date,
                              account_id, notes, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (v["date"], v["category"], v["label"], v["amount"], v["lot_id"], v["beneficiary"], v["status"],
                            v["date"] if v["status"] == "paye" else "", v["account_id"], v["notes"], g.user["id"], now_utc()))
        exp_id = cur.lastrowid
        if v["status"] == "paye":
            _cash(conn, exp_id, v["date"], v["account_id"], v["amount"], f"Dépense : {v['category']} {v['label']}".strip())
    lot = query("SELECT name FROM lots WHERE id = ?", (v["lot_id"],), one=True) if v["lot_id"] else None
    what = f"{v['category']}" + (f" — {v['label']}" if v["label"] else "")
    log_activity("Dépense", f"{what} : {fmt_money(v['amount'])}")
    notify("expense", exp_id, f"🧾 Dépense : {what} · {fmt_money(v['amount'])} le {date_fr(v['date'])} · "
           + (f"lot « {lot['name']} »" if lot else "commune (partagée entre les lots)")
           + (" · PAS ENCORE PAYÉE" if v["status"] == "a_payer" else ""))
    flash("Dépense enregistrée." + (" Elle reste « à payer » (en rouge) jusqu'à ce que vous la payiez." if v["status"] == "a_payer"
                                    else " L'argent est sorti de la caisse choisie."), "success")
    return redirect(url_for("depenses.index") + f"#dep-{exp_id}")


@bp.route("/<int:exp_id>")
@require("depenses", VIEW)
def item(exp_id):
    _get(exp_id)
    return redirect(url_for("depenses.index", voir="tout") + f"#dep-{exp_id}")


def _get(exp_id):
    row = query("SELECT * FROM expenses WHERE id = ?", (exp_id,), one=True)
    if row is None:
        abort(404)
    return row


@bp.route("/<int:exp_id>/modifier", methods=["GET", "POST"])
@require("depenses", EDIT)
def edit(exp_id):
    item = _get(exp_id)
    if not can_correct("depenses", item):
        abort(403)
    if request.method == "POST":
        v, errors = _form()
        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("depenses.edit", exp_id=exp_id))
        paid_date = (item["paid_date"] or v["date"]) if v["status"] == "paye" else ""
        with transaction() as conn:
            conn.execute("""UPDATE expenses SET date = ?, category = ?, label = ?, amount = ?, lot_id = ?, beneficiary = ?, status = ?,
                            paid_date = ?, account_id = ?, notes = ? WHERE id = ?""",
                         (v["date"], v["category"], v["label"], v["amount"], v["lot_id"], v["beneficiary"], v["status"],
                          paid_date, v["account_id"], v["notes"], exp_id))
            _cash(conn, exp_id, paid_date, v["account_id"] if v["status"] == "paye" else None, v["amount"],
                  f"Dépense : {v['category']} {v['label']}".strip())
        log_activity("Dépense corrigée", f"n°{exp_id}")
        notify("expense", exp_id, f"✏️ Dépense n°{exp_id} corrigée : {v['category']} · {fmt_money(item['amount'])} → {fmt_money(v['amount'])}.")
        flash("Dépense corrigée.", "success")
        return redirect(url_for("depenses.index") + f"#dep-{exp_id}")
    lots = query("SELECT id, name FROM lots ORDER BY status = 'actif' DESC, arrival_date DESC")
    return render_template("depenses/form.html", item=item, cats=categories(), accounts=accounts(), lots=lots, today=today())


@bp.route("/<int:exp_id>/payer", methods=["POST"])
@require("depenses", EDIT)
def pay(exp_id):
    item = _get(exp_id)
    date = request.form.get("date") or today()
    account = query("SELECT * FROM accounts WHERE id = ?", (request.form.get("account_id") or 0,), one=True)
    if item["status"] != "a_payer":
        flash("Cette dépense est déjà payée.", "info")
    elif account is None or not valid_date(date) or date > today():
        flash("Choisissez la caisse et une date valide.", "error")
    else:
        with transaction() as conn:
            conn.execute("UPDATE expenses SET status = 'paye', paid_date = ?, account_id = ? WHERE id = ?", (date, account["id"], exp_id))
            _cash(conn, exp_id, date, account["id"], item["amount"], f"Dépense : {item['category']} {item['label']}".strip())
        notify("expense", exp_id, f"💸 Dépense payée : {item['category']} {item['label']} · {fmt_money(item['amount'])} "
               f"(caisse « {account['name']} », le {date_fr(date)}).")
        flash("Dépense payée : l'argent est sorti de la caisse.", "success")
    return redirect(request.form.get("next") or url_for("depenses.index") + f"#dep-{exp_id}")


@bp.route("/<int:exp_id>/supprimer", methods=["POST"])
@require("depenses", EDIT)
def delete(exp_id):
    item = _get(exp_id)
    if not can_correct("depenses", item):
        abort(403)
    with transaction() as conn:
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'expense' AND ref_id = ?", (exp_id,))
        conn.execute("DELETE FROM expenses WHERE id = ?", (exp_id,))
    log_activity("Dépense supprimée", f"{item['category']} {fmt_money(item['amount'])}")
    notify("", None, f"↩️ Dépense supprimée : {item['category']} {item['label']} · {fmt_money(item['amount'])} du {date_fr(item['date'])}.")
    flash("Dépense supprimée (l'argent est revenu dans la caisse).", "success")
    return redirect(url_for("depenses.index"))


__all__ = ["bp", "alerts", "total_unpaid", "categories", "CATEGORIES"]
