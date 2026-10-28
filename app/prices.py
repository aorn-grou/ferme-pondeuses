"""Prix des œufs du jour.

Le prix d'un plateau change souvent (selon la saison, le marché, le client…).
On écrit le nouveau prix le jour où il change : il reste valable les jours suivants,
jusqu'au prochain changement. Chaque changement est gardé, pour voir la variation
par jour, par semaine, par mois et par année, et la comparer au coût de revient.
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query
from .security import EDIT, MANAGE, VIEW, can, require
from .stock import parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("prix", __name__, url_prefix="/prix-oeufs")

TRAY = 30
VIEWS = {"jour": "Jour", "semaine": "Semaine", "mois": "Mois", "annee": "Année"}
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _d(value):
    return Date.fromisoformat(value)


def price_on(day=None):
    """Prix du plateau valable ce jour-là (le dernier prix écrit avant ou ce jour), ou None."""
    row = query("SELECT * FROM egg_prices WHERE date <= ? ORDER BY date DESC, id DESC LIMIT 1", (day or today(),), one=True)
    return row["tray_price"] if row else None


def price_row(day=None):
    return query("SELECT * FROM egg_prices WHERE date <= ? ORDER BY date DESC, id DESC LIMIT 1", (day or today(),), one=True)


def _daily_prices(start, end):
    """{jour: prix du plateau} pour chaque jour de start à end (le prix reste jusqu'au changement suivant)."""
    rows = query("SELECT date, tray_price FROM egg_prices WHERE date <= ? ORDER BY date, id", (end.isoformat(),))
    out, cur, i = {}, None, 0
    rows = list(rows)
    d = start
    # dernier prix connu avant le début
    while i < len(rows) and rows[i]["date"] < start.isoformat():
        cur = rows[i]["tray_price"]
        i += 1
    while d <= end:
        while i < len(rows) and rows[i]["date"] <= d.isoformat():
            cur = rows[i]["tray_price"]
            i += 1
        out[d.isoformat()] = cur
        d += timedelta(days=1)
    return out


def _key(day, view):
    d = _d(day)
    if view == "jour":
        return day
    if view == "semaine":
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat()
    if view == "mois":
        return day[:7]
    return day[:4]


def _label(key, view):
    if view == "jour":
        return f"{key[8:10]}/{key[5:7]}"
    if view == "semaine":
        return f"sem. {key[8:10]}/{key[5:7]}"
    if view == "mois":
        return f"{MONTHS[int(key[5:7]) - 1]} {key[2:4]}"
    return key


def _range(view):
    end = _d(today())
    if view == "jour":
        start = end - timedelta(days=29)
    elif view == "semaine":
        start = end - timedelta(days=end.weekday()) - timedelta(weeks=11)
    elif view == "mois":
        y, m = end.year, end.month - 11
        while m <= 0:
            m += 12
            y -= 1
        start = Date(y, m, 1)
    else:
        first = query("""SELECT MIN(d) AS d FROM (SELECT MIN(date) AS d FROM egg_prices UNION ALL SELECT MIN(date) FROM sales
                         UNION ALL SELECT MIN(date) FROM egg_collections)""", one=True)["d"]
        start = Date(_d(first).year if first else end.year, 1, 1)
    return start, end


def period_stats(view):
    """Pour chaque période : prix fixé (moyen, min, max), prix réellement vendu, coût de revient d'un plateau,
    œufs ramassés / vendus, ventes et bénéfice (ventes d'œufs − provende − charges de la période)."""
    from .costs import charges

    start, end = _range(view)
    s, e = start.isoformat(), end.isoformat()
    days = _daily_prices(start, end)
    keys = []
    for day in days:
        k = _key(day, view)
        if k not in keys:
            keys.append(k)
    st = {k: {"key": k, "label": _label(k, view), "prices": [], "sold": 0, "revenue": 0.0, "laid": 0, "feed": 0.0, "charges": 0.0}
          for k in keys}
    for day, p in days.items():
        if p is not None:
            st[_key(day, view)]["prices"].append(p)
    for r in query("SELECT date, quantity, total FROM sales WHERE is_eggs = 1 AND date BETWEEN ? AND ?", (s, e)):
        x = st.get(_key(r["date"], view))
        if x:
            x["sold"] += r["quantity"] or 0
            x["revenue"] += r["total"] or 0
    for r in query("SELECT date, SUM(good + broken) AS n FROM egg_collections WHERE date BETWEEN ? AND ? GROUP BY date", (s, e)):
        x = st.get(_key(r["date"], view))
        if x:
            x["laid"] += r["n"] or 0
    for r in query("SELECT date, SUM(quantity * unit_cost) AS c FROM feedings WHERE date BETWEEN ? AND ? GROUP BY date", (s, e)):
        x = st.get(_key(r["date"], view))
        if x:
            x["feed"] += r["c"] or 0
    for day, amount, _kind, _lot in charges(e):
        if s <= day <= e:
            x = st.get(_key(day, view))
            if x:
                x["charges"] += amount
    rows = []
    for k in keys:
        x = st[k]
        pr = x["prices"]
        x["avg"] = sum(pr) / len(pr) if pr else None
        x["min"] = min(pr) if pr else None
        x["max"] = max(pr) if pr else None
        x["sold_tray"] = x["revenue"] / x["sold"] * TRAY if x["sold"] else None
        x["costs"] = x["feed"] + x["charges"]
        x["cost_tray"] = x["costs"] / x["laid"] * TRAY if x["laid"] else None
        ref = x["sold_tray"] or x["avg"]
        x["margin_tray"] = (ref - x["cost_tray"]) if (ref is not None and x["cost_tray"] is not None) else None
        x["profit"] = x["revenue"] - x["costs"]
        rows.append(x)
    return rows


def _charts(rows, view):
    labels = [r["label"] for r in rows]
    price = {"labels": labels, "unit": "Ar", "series": [
        {"name": "Prix fixé (plateau)", "values": [round(r["avg"]) if r["avg"] is not None else None for r in rows], "color": "#c9971c"},
        {"name": "Prix réellement vendu", "values": [round(r["sold_tray"]) if r["sold_tray"] else None for r in rows], "color": "#2e7d32"},
        {"name": "Coût de revient", "values": [round(r["cost_tray"]) if r["cost_tray"] else None for r in rows], "color": "#c0352b",
         "dash": True},
    ]}
    profit = {"labels": labels, "values": [round(r["profit"]) for r in rows], "unit": "Ar", "allowNegative": True,
              "color": "#2e7d32"}
    return price, profit


@bp.route("/")
@require("ventes", VIEW)
def index():
    view = request.args.get("vue", "jour")
    if view not in VIEWS:
        view = "jour"
    now = price_row()
    prev = query("SELECT * FROM egg_prices WHERE date < ? ORDER BY date DESC, id DESC LIMIT 1",
                 (now["date"],), one=True) if now else None
    rows = period_stats(view)
    price_chart, profit_chart = _charts(rows, view)
    with_price = [r for r in rows if r["avg"] is not None]
    summary = {
        "avg": sum(r["avg"] for r in with_price) / len(with_price) if with_price else None,
        "min": min((r["min"] for r in with_price), default=None),
        "max": max((r["max"] for r in with_price), default=None),
        "profit": sum(r["profit"] for r in rows),
        "revenue": sum(r["revenue"] for r in rows),
    }
    history = query("""SELECT p.*, u.username FROM egg_prices p LEFT JOIN users u ON u.id = p.created_by
                       ORDER BY p.date DESC, p.id DESC LIMIT 60""")
    return render_template("prix/index.html", now=now, prev=prev, rows=rows, view=view, views=VIEWS, history=history,
                           price_chart=price_chart, profit_chart=profit_chart, summary=summary, tray=TRAY, today=today(),
                           has_data=any(r["sold"] or r["laid"] or r["avg"] is not None for r in rows))


@bp.route("/changer", methods=["POST"])
@require("ventes", EDIT)
def set_price():
    day = request.form.get("date") or today()
    price = parse_num(request.form.get("tray_price"))
    unit = request.form.get("unit", "plateau")
    notes = " ".join(request.form.get("notes", "").split())[:150]
    back = request.form.get("next") or url_for("prix.index")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("prix.index")
    if not valid_date(day) or day > today():
        flash("Date invalide (pas de date dans le futur).", "error")
        return redirect(back)
    if price is None or price <= 0:
        flash("Écrivez le prix (un nombre plus grand que 0).", "error")
        return redirect(back)
    tray_price = price * TRAY if unit == "oeuf" else price
    old = query("SELECT * FROM egg_prices WHERE date = ?", (day,), one=True)
    if old:
        execute("UPDATE egg_prices SET tray_price = ?, notes = ?, created_by = ?, created_at = ? WHERE id = ?",
                (tray_price, notes, g.user["id"], now_utc(), old["id"]))
    else:
        execute("INSERT INTO egg_prices (date, tray_price, notes, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (day, tray_price, notes, g.user["id"], now_utc()))
    log_activity("Prix des œufs", f"{date_fr(day)} : {fmt_money(tray_price)} le plateau" + (f" ({notes})" if notes else ""))
    flash(f"Prix enregistré : {fmt_money(tray_price)} le plateau ({fmt_money(tray_price / TRAY)} l'œuf) à partir du "
          f"{date_fr(day)}. Les nouvelles ventes le proposent tout seul.", "success")
    return redirect(back)


@bp.route("/<int:price_id>/supprimer", methods=["POST"])
@require("ventes", MANAGE)
def delete(price_id):
    row = query("SELECT * FROM egg_prices WHERE id = ?", (price_id,), one=True)
    if row is None:
        abort(404)
    execute("DELETE FROM egg_prices WHERE id = ?", (price_id,))
    log_activity("Prix des œufs supprimé", f"{date_fr(row['date'])} : {fmt_money(row['tray_price'])}")
    flash("Prix supprimé. Les ventes déjà faites ne changent pas.", "success")
    return redirect(url_for("prix.index"))


__all__ = ["bp", "price_on", "price_row"]
