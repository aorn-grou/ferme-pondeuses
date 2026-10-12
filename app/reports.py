"""Rapports : les diagnostics de la ferme, en schémas.

- Argent : ventes, dépenses et résultat, mois par mois.
- Chaque lot : œufs et provende depuis le jour 1, bénéfice ou perte, coût d'un œuf.
- Rentabilité : le seuil de ponte qui paie la provende, et jusqu'à quand chaque lot reste rentable (estimation).
- Pics, creux et moyennes : taux de ponte, œufs, ventes, mortalité.
Tout est calculé à partir des saisies : rien à remplir ici.
"""
from datetime import date as Date, timedelta

from flask import Blueprint, render_template, request

from .db import query
from .security import VIEW, can, require
from .stock import today

bp = Blueprint("rapports", __name__, url_prefix="/rapports")

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _d(v):
    return Date.fromisoformat(v)


def _mlabel(key):
    return f"{MONTHS[int(key[5:7]) - 1]} {key[2:4]}"


def _months(n):
    end = _d(today()).replace(day=1)
    keys = []
    y, m = end.year, end.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def _peaks(values, labels):
    pts = [(v, labels[i]) for i, v in enumerate(values) if v is not None and v > 0]
    if not pts:
        return None
    hi, lo = max(pts), min(pts)
    return {"hi": hi[0], "hi_at": hi[1], "lo": lo[0], "lo_at": lo[1], "avg": sum(v for v, _ in pts) / len(pts),
            "last": pts[-1][0], "count": len(pts)}


# ---------------------------------------------------------------------------
# Argent
# ---------------------------------------------------------------------------
def money_by_month(n=12):
    keys = _months(n)
    since = keys[0] + "-01"

    def by(sql):
        return {r["k"]: r["v"] or 0 for r in query(sql, (since,))}
    sales = by("SELECT substr(date, 1, 7) AS k, SUM(total) AS v FROM sales WHERE date >= ? GROUP BY k")
    buys = by("SELECT substr(date, 1, 7) AS k, SUM(total) AS v FROM purchases WHERE date >= ? GROUP BY k")
    chicks = by("SELECT substr(arrival_date, 1, 7) AS k, SUM(initial_count * chick_price + other_costs) AS v FROM lots "
                "WHERE arrival_date >= ? GROUP BY k")
    other = by("SELECT substr(date, 1, 7) AS k, -SUM(amount) AS v FROM cash_movements WHERE amount < 0 AND date >= ? "
               "AND kind NOT IN ('achat', 'achat_poussins', 'reglement', 'reglement_fournisseur') AND COALESCE(ref_type, '') NOT IN ('purchase', 'lot', 'supplier_payment') GROUP BY k")
    rows, cum = [], 0
    for k in keys:
        out = buys.get(k, 0) + chicks.get(k, 0) + other.get(k, 0)
        res = sales.get(k, 0) - out
        cum += res
        rows.append({"key": k, "label": _mlabel(k), "sales": sales.get(k, 0), "out": out, "buys": buys.get(k, 0),
                     "chicks": chicks.get(k, 0), "other": other.get(k, 0), "result": res, "cum": cum})
    # on enlève les mois vides du début
    while rows and not rows[0]["sales"] and not rows[0]["out"]:
        rows.pop(0)
    cum = 0
    for r in rows:
        cum += r["result"]
        r["cum"] = cum
    labels = [r["label"] for r in rows]
    chart = {"labels": labels, "unit": "Ar", "series": [
        {"name": "Ventes", "values": [round(r["sales"]) for r in rows], "color": "#2e7d32"},
        {"name": "Dépenses", "values": [round(r["out"]) for r in rows], "color": "#c0352b"},
    ], "peaks": False}
    cum_chart = {"labels": labels, "values": [round(r["cum"]) for r in rows], "unit": "Ar", "color": "#c9971c", "allowNegative": True}
    return {"rows": rows, "chart": chart, "cum_chart": cum_chart,
            "totals": {k: sum(r[k] for r in rows) for k in ("sales", "out", "result", "buys", "chicks", "other")}}


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------
def _std_rate(week_since_lay):
    """Courbe de ponte type (poule pondeuse) : montée en 8 semaines, pic ~93 %, puis lente baisse."""
    if week_since_lay < 0:
        return 0.0
    if week_since_lay < 8:
        return 0.12 + 0.81 * week_since_lay / 8
    return max(0.0, 0.93 - 0.0035 * (week_since_lay - 8))


def lot_report(lot):
    """Tout sur un lot depuis le jour 1 : semaine par semaine, œufs, provende, argent, + estimation de la suite."""
    from .eggs import lot_egg_stats
    from .lots import date_of_week, forecast, lot_summary, lot_week
    from .sales import lot_revenue

    s, cons = lot_summary(lot)
    eggs = lot_egg_stats(lot)
    rev = lot_revenue(lot)
    week_now = lot_week(lot)
    first_week = max(1, (lot["age_days_at_arrival"] or 0) // 7 + 1)
    end_week = (lot["reform_week"] or 72) if lot["status"] == "actif" else lot_week(lot, lot["end_date"] or today())
    weeks = list(range(first_week, max(end_week, week_now) + 1))
    feed_kg, feed_cost, laid, deaths, rate = {}, {}, {}, {}, {}
    for r in cons["rows"]:
        w = lot_week(lot, r["date"])
        feed_kg[w] = feed_kg.get(w, 0) + r["quantity"]
        feed_cost[w] = feed_cost.get(w, 0) + r["quantity"] * r["unit_cost"]
    rate_sum = {}
    for r in eggs["rows"]:
        w = lot_week(lot, r["date"])
        laid[w] = laid.get(w, 0) + r["laid"]
        rate_sum.setdefault(w, []).append(r["rate"])
    for w, v in rate_sum.items():
        rate[w] = sum(v) / len(v)
    for e in query("SELECT date, -quantity AS n FROM lot_events WHERE lot_id = ? AND kind = 'mort'", (lot["id"],)):
        w = lot_week(lot, e["date"])
        deaths[w] = deaths.get(w, 0) + e["n"]

    price = (rev["total"] / eggs["good"]) if eggs["good"] and rev["total"] else (lot["egg_price"] or 0)
    lw = lot["laying_week"] or 18
    # estimation de la suite (lots en cours) : courbe type ajustée au niveau réel du lot
    fc = forecast(lot) if lot["status"] == "actif" else {"weeks": []}
    future = {w["week"]: w for w in fc["weeks"]}
    ratio = 1.0
    if eggs["avg7"] is not None and week_now >= lw + 2 and _std_rate(week_now - lw) > 0:
        ratio = max(0.5, min(1.3, (eggs["avg7"] / 100) / _std_rate(week_now - lw)))
    feed_price = 0
    if fc.get("kg"):
        feed_price = fc["cost"] / fc["kg"]
    elif cons["kg"]:
        feed_price = cons["cost"] / cons["kg"]
    proj_rate, proj_margin, actual_margin = {}, {}, {}
    for w in weeks:
        if w in laid or w in feed_cost:
            actual_margin[w] = laid.get(w, 0) * price - feed_cost.get(w, 0)
        if w in future and w > week_now - 1:
            pr = min(0.97, _std_rate(w - lw) * ratio)
            proj_rate[w] = pr * 100
            fw = future[w]
            proj_margin[w] = fw["birds"] * pr * fw["days"] * price - fw["kg"] * (fw["cost"] / fw["kg"] if fw["kg"] else feed_price)
    # seuil : taux de ponte qui paie juste la provende (g/poule/jour × prix du kg ÷ prix d'un œuf)
    grams = s.get("suggest", {}).get("grams") or 115
    threshold = (grams / 1000 * feed_price / price * 100) if price and feed_price else None
    end_profit = None
    for w in sorted(proj_margin):
        if w >= lw + 8 and proj_margin[w] < 0:
            end_profit = {"week": w, "date": date_of_week(lot, w).isoformat()}
            break
    feed_until_end = sum(f["kg"] for w, f in future.items() if not end_profit or w < end_profit["week"])
    labels = [f"S{w}" for w in weeks]
    lay_mark = [{"at": f"S{lw}", "text": f"🥚 Début de ponte (S{lw})"}]
    eggs_vs_feed = {"labels": labels, "bars": [round(feed_kg.get(w, 0), 1) for w in weeks],
                    "line": [laid.get(w) for w in weeks], "unit": "kg", "barLabel": "Provende mangée",
                    "lineLabel": "Œufs pondus", "lineUnit": "œufs", "marks": lay_mark, "barColor": "#3a8a2e", "lineColor": "#c9971c"}
    margin_chart = {"labels": labels, "unit": "Ar", "allowNegative": True, "marks": lay_mark + (
        [{"at": f"S{end_profit['week']}", "text": "⚠️ plus rentable", "color": "#c0352b"}] if end_profit else []), "series": [
        {"name": "Marge réelle (œufs − provende)", "values": [round(actual_margin[w]) if w in actual_margin else None for w in weeks], "color": "#2e7d32"},
        {"name": "Estimation", "values": [round(proj_margin[w]) if w in proj_margin else None for w in weeks], "color": "#c9971c", "dash": True},
    ], "peaks": False, "now": f"S{week_now}"}
    rate_chart = {"labels": labels, "unit": "%", "max": 100, "marks": lay_mark, "now": f"S{week_now}", "series": [
        {"name": "Taux réel", "values": [round(rate[w], 1) if w in rate else None for w in weeks], "color": "#2e7d32"},
        {"name": "Estimation", "values": [round(proj_rate[w], 1) if w in proj_rate else None for w in weeks], "color": "#c9971c", "dash": True},
    ] + ([{"name": "Seuil de rentabilité", "values": [round(threshold, 1)] * len(weeks), "color": "#c0352b", "dash": True, "thin": True}] if threshold else [])}
    costs = s["chick_cost"] + s["eaten_cost"]
    feed_since_lay = sum(feed_cost.get(w, 0) for w in weeks if w >= lw)
    return {
        "lot": lot, "s": s, "eggs": eggs, "rev": rev, "price": price, "costs": costs, "result": rev["total"] - costs,
        "feed_per_egg": (feed_since_lay / eggs["laid"]) if eggs["laid"] else None,
        "cost_per_egg": (costs / eggs["laid"]) if eggs["laid"] else None,
        "g_per_egg": (sum(feed_kg.get(w, 0) for w in weeks if w >= lw) * 1000 / eggs["laid"]) if eggs["laid"] else None,
        "threshold": threshold, "end_profit": end_profit, "feed_until_end": feed_until_end, "feed_price": feed_price,
        "eggs_vs_feed": eggs_vs_feed, "margin_chart": margin_chart, "rate_chart": rate_chart, "week": week_now,
        "deaths": sum(deaths.values()), "mortality": (sum(deaths.values()) / lot["initial_count"] * 100) if lot["initial_count"] else 0,
        "rate_peaks": _peaks([rate.get(w) for w in weeks], labels),
    }


# ---------------------------------------------------------------------------
# Tendances : pics, creux et moyennes, mois par mois
# ---------------------------------------------------------------------------
def trends(n=12):
    keys = _months(n)
    since = keys[0] + "-01"
    eggs = {r["k"]: r for r in query("SELECT substr(date, 1, 7) AS k, SUM(good + broken) AS laid, COUNT(DISTINCT date) AS days "
                                     "FROM egg_collections WHERE date >= ? GROUP BY k", (since,))}
    sales = {r["k"]: r for r in query("SELECT substr(date, 1, 7) AS k, SUM(total) AS t, SUM(CASE WHEN is_eggs = 1 THEN quantity ELSE 0 END) AS q "
                                      "FROM sales WHERE date >= ? GROUP BY k", (since,))}
    deaths = {r["k"]: r["n"] for r in query("SELECT substr(date, 1, 7) AS k, -SUM(quantity) AS n FROM lot_events WHERE kind = 'mort' "
                                            "AND date >= ? GROUP BY k", (since,))}
    feed = {r["k"]: r["q"] for r in query("SELECT substr(date, 1, 7) AS k, SUM(quantity) AS q FROM feedings WHERE date >= ? GROUP BY k", (since,))}
    rows = []
    for k in keys:
        e, s = eggs.get(k), sales.get(k)
        rows.append({"key": k, "label": _mlabel(k), "eggs_day": (e["laid"] / e["days"]) if e and e["days"] else None,
                     "laid": e["laid"] if e else 0, "sales": s["t"] if s else 0,
                     "price": (s["t"] / s["q"] * 30) if s and s["q"] else None, "deaths": deaths.get(k, 0), "feed": feed.get(k, 0)})
    while rows and not rows[0]["laid"] and not rows[0]["sales"] and not rows[0]["feed"]:
        rows.pop(0)
    cur = today()[:7]
    for r in rows:
        if r["key"] == cur:
            r["label"] += "*"  # mois en cours (pas encore fini)
    labels = [r["label"] for r in rows]

    def chart(key, unit, color, rnd=0):
        vals = [r[key] for r in rows]
        start = next((i for i, v in enumerate(vals) if v), len(vals))  # on commence au premier mois avec des données
        return {"labels": labels[start:], "values": [round(v or 0, rnd) for v in vals[start:]], "unit": unit, "color": color}
    return {"rows": rows, "labels": labels, "partial": any(r["key"] == cur for r in rows),
            "sales_chart": chart("sales", "Ar", "#2e7d32"),
            "eggs": _peaks([r["eggs_day"] for r in rows], labels), "price": _peaks([r["price"] for r in rows], labels),
            "sales": _peaks([r["sales"] if r["key"] != cur else None for r in rows], labels),
            "deaths": _peaks([r["deaths"] if r["key"] != cur else None for r in rows], labels),
            "eggs_chart": chart("eggs_day", "œufs/jour", "#c9971c"), "deaths_chart": chart("deaths", "morts", "#c0352b"),
            "price_chart": chart("price", "Ar", "#1f6fa8")}


@bp.route("/")
@require("rapports", VIEW)
def index():
    lots = query("SELECT * FROM lots ORDER BY status = 'actif' DESC, arrival_date")
    reports = [lot_report(l) for l in lots] if can("lots") else []
    sel = request.args.get("lot", type=int)
    current = next((r for r in reports if r["lot"]["id"] == sel), None) or next(
        (r for r in reports if r["eggs"]["laid"]), reports[0] if reports else None)
    return render_template("rapports/index.html", money=money_by_month(12), reports=reports, current=current,
                           trends=trends(12))


__all__ = ["bp", "lot_report", "money_by_month", "trends"]
