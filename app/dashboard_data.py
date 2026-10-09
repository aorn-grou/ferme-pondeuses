"""Chiffres du tableau de bord : tout ce qu'il faut voir d'un coup d'œil, calculé sur les vraies données."""
import json
from datetime import date as Date, timedelta

from .db import query
from .security import can
from .stock import feed_stock, formulas_overview, materials_overview, supplier_balance, today


def _days(n):
    end = Date.fromisoformat(today())
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _series(sql, days, params=()):
    rows = {r["d"]: r["v"] for r in query(sql, (days[0], *params))}
    return [round(rows.get(d, 0) or 0, 2) for d in days]


def _weeks(series, size=7):
    return [round(sum(series[i:i + size]), 2) for i in range(0, len(series), size)]


def _label(d):
    return f"{d[8:10]}/{d[5:7]}"


MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _plan(cards):
    """Provende à venir de tous les lots, répartie par mois, avec le détail du calcul par lot."""
    months, rows = {}, []
    for card in cards:
        fc = card["s"].get("forecast") or {}
        if not fc.get("weeks") or not fc.get("kg"):
            continue
        lot = card["lot"]
        for w in fc["weeks"]:
            if not w["days"]:
                continue
            per_day, cost_day = w["kg"] / w["days"], w["cost"] / w["days"]
            start = Date.fromisoformat(w["start"])
            for i in range(w["days"]):
                key = (start + timedelta(days=i)).isoformat()[:7]
                m = months.setdefault(key, {"kg": 0.0, "cost": 0.0, "lots": {}})
                m["kg"] += per_day
                m["cost"] += cost_day
                m["lots"][lot["name"]] = m["lots"].get(lot["name"], 0) + per_day
        rows.append({"id": lot["id"], "name": lot["name"], "birds": card["s"]["birds"], "week": card["s"]["week"],
                     "reform": fc["end_week"], "end_date": fc["end_date"], "days": fc["days"], "kg": fc["kg"],
                     "cost": fc["cost"],
                     "parts": [{"name": n, **f} for n, f in fc["by_formula"].items()]})
    keys = sorted(months)
    labels = [f"{MONTHS[int(k[5:7]) - 1]} {k[2:4]}" for k in keys]
    tips = []
    for k in keys:
        m = months[k]
        detail = " · ".join(f"{n} {round(v):,}".replace(",", " ") + " kg" for n, v in m["lots"].items())
        tips.append(f"{round(m['cost']):,}".replace(",", " ") + f" Ar<br><small>{detail}</small>")
    from .lots import date_of_week

    marks = {}
    for card in cards:
        lot = card["lot"]
        if card["s"].get("laying", {}).get("phase") == "élevage":
            k = date_of_week(lot, lot["laying_week"] or 18).isoformat()[:7]
            if k in months:
                lab = f"{MONTHS[int(k[5:7]) - 1]} {k[2:4]}"
                marks[lab] = (marks.get(lab, "🥚") + " " + lot["name"]).strip()
    chart = {"labels": labels, "bars": [round(months[k]["kg"], 1) for k in keys], "line": [None] * len(keys),
             "marks": [{"at": a, "text": t + " pond"} for a, t in marks.items()],
             "unit": "kg", "barLabel": "À manger", "barColor": "#c9971c", "tips": tips,
             "now": labels[0] if labels else "", "nowLabel": "Ce mois"}
    return {"rows": rows, "chart": chart}


def build():
    days30 = _days(30)
    days56 = _days(56)
    labels30 = [_label(d) for d in days30]
    out = {"labels30": labels30}

    # --- Lots et poules -------------------------------------------------------
    if can("lots"):
        from .lots import lot_summary

        lots = query("SELECT l.*, p.name AS program FROM lots l LEFT JOIN feed_programs p ON p.id = l.program_id "
                     "WHERE l.status = 'actif' ORDER BY l.arrival_date")
        cards = []
        for lot in lots:
            s, _cons = lot_summary(lot)
            cards.append({"lot": lot, "s": s})
        deaths = _series("SELECT date AS d, -SUM(quantity) AS v FROM lot_events WHERE kind = 'mort' AND date >= ? GROUP BY date",
                         days30)
        out["lots"] = {
            "cards": cards,
            "count": len(cards),
            "birds": sum(c["s"]["birds"] for c in cards),
            "today_kg": sum(c["s"].get("suggest", {}).get("kg", 0) for c in cards),
            "deaths7": sum(deaths[-7:]),
            "deaths_prev7": sum(deaths[-14:-7]),
            "future_kg": sum(c["s"].get("forecast", {}).get("kg", 0) for c in cards),
            "future_cost": sum(c["s"].get("forecast", {}).get("cost", 0) for c in cards),
            "plan": _plan(cards),
        }

    # --- Œufs ---------------------------------------------------------------
    if can("oeufs") and out.get("lots"):
        from .eggs import farm_chart, lot_egg_stats

        lots_active = [c["lot"] for c in out["lots"]["cards"]]
        ch = farm_chart(lots_active)
        rates7 = [st["avg7"] for st in (lot_egg_stats(l) for l in lots_active) if st["avg7"] is not None]
        tday = today()
        today_rows = query("SELECT COALESCE(SUM(good + broken), 0) AS n FROM egg_collections WHERE date = ?", (tday,), one=True)["n"]
        out["eggs"] = {"chart": ch, "today": today_rows,
                       "week": query("SELECT COALESCE(SUM(good + broken), 0) AS n FROM egg_collections WHERE date >= ?",
                                     (_days(7)[0],), one=True)["n"],
                       "prev_week": query("SELECT COALESCE(SUM(good + broken), 0) AS n FROM egg_collections WHERE date >= ? AND date < ?",
                                          (_days(14)[0], _days(7)[0]), one=True)["n"],
                       "rate7": (sum(rates7) / len(rates7)) if rates7 else None,
                       "spark": ch["eggs"]["values"][-28:] if ch else []}
        for card in out["lots"]["cards"]:
            card["eggs"] = lot_egg_stats(card["lot"])

    # --- Ventes -------------------------------------------------------------
    if can("ventes"):
        from .sales import egg_stock, sales_rows, total_due

        m_rows = sales_rows(since=today()[:7] + "-01")
        out["sales"] = {"month": sum(r["total"] for r in m_rows), "count": len(m_rows), "due": total_due(),
                        "stock": egg_stock(),
                        "weeks": _weeks(_series("SELECT date AS d, SUM(total) AS v FROM sales WHERE date >= ? GROUP BY date", days56))}

    # --- Provende ------------------------------------------------------------
    if can("provenderie") or can("alimentation"):
        given = _series("SELECT date AS d, SUM(quantity) AS v FROM feedings WHERE date >= ? GROUP BY date", days30)
        made = _series("SELECT date AS d, SUM(quantity) AS v FROM productions WHERE date >= ? GROUP BY date", days30)
        given56 = _series("SELECT date AS d, SUM(quantity) AS v FROM feedings WHERE date >= ? GROUP BY date", days56)
        # besoin par jour de chaque provende : ce que les lots doivent manger aujourd'hui, sinon la moyenne donnée
        need = {}
        for card in out.get("lots", {}).get("cards", []):
            sug = card["s"].get("suggest") or {}
            if sug.get("formula_id"):
                need[sug["formula_id"]] = need.get(sug["formula_id"], 0) + sug["kg"]
        avg14 = {r["formula_id"]: (r["q"] or 0) / 14 for r in query(
            "SELECT formula_id, SUM(quantity) AS q FROM feedings WHERE date >= ? GROUP BY formula_id", (_days(14)[0],))}
        feeds = []
        for f in formulas_overview():
            per_day = need.get(f["id"]) or avg14.get(f["id"]) or 0
            days_left = (f["stock"] / per_day) if per_day > 0 else None
            if f["stock"] > 0 or per_day > 0:
                feeds.append({"id": f["id"], "name": f["name"], "stock": f["stock"], "per_day": per_day,
                              "days": days_left, "value": f["value"]})
        out["feed"] = {
            "given": given, "made": made, "given_weeks": _weeks(given56),
            "given_month": sum(given), "made_month": sum(made),
            "stock": sum(max(0, f["stock"]) for f in feeds), "value": sum(f["value"] for f in feeds),
            "items": sorted(feeds, key=lambda f: (f["days"] is None, f["days"] or 0)),
        }
        out["feed"]["min_days"] = min([f["days"] for f in feeds if f["days"] is not None], default=None)

    # --- Matières premières et argent ---------------------------------------
    if can("matieres"):
        mats = materials_overview()
        buys56 = _series("SELECT date AS d, SUM(total) AS v FROM purchases WHERE date >= ? GROUP BY date", days56)
        out["materials"] = {
            "items": mats,
            "value": sum(m["value"] for m in mats),
            "alerts": [m for m in mats if m["status"] in ("low", "empty")],
            "month_buys": query("SELECT COALESCE(SUM(total), 0) AS t FROM purchases WHERE substr(date, 1, 7) = ?",
                                (today()[:7],), one=True)["t"],
            "buy_weeks": _weeks(buys56),
            "due": sum(max(0, supplier_balance(s["id"])["due"]) for s in query("SELECT id FROM suppliers")),
        }
        for m in mats:  # remplissage de la barre : stock comparé au seuil (ou au plus gros stock)
            ref = max(m["alert_threshold"] * 3 if m["alert_threshold"] else 0, m["stock"], 1)
            m["fill"] = max(2, min(100, m["stock"] / ref * 100))
    if can("matieres") or can("caisse"):
        out["accounts"] = query("""SELECT a.name, COALESCE(SUM(c.amount), 0) AS balance FROM accounts a
                                   LEFT JOIN cash_movements c ON c.account_id = a.id WHERE a.active = 1
                                   GROUP BY a.id ORDER BY a.sort, a.name""")
        out["month_out"] = -query("SELECT COALESCE(SUM(amount), 0) AS t FROM cash_movements WHERE amount < 0 "
                                  "AND substr(date, 1, 7) = ?", (today()[:7],), one=True)["t"]

    # --- Écarts à surveiller (vols, pertes, mortalité anormale) --------------
    if can("controles"):
        from .controls import summary

        out["controls"] = summary(30)

    # --- Derniers signalements ----------------------------------------------
    from .discussion import _feed

    out["news"] = [m for m in _feed(limit=40) if m.get("auto")][-6:][::-1]
    return out


def chart(values, labels=None, **extra):
    """Données d'un graphique, prêtes pour l'attribut data-chart."""
    data = {"values": values, "labels": labels or [], **extra}
    return json.dumps(data)


__all__ = ["build", "chart", "feed_stock"]
