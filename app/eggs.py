"""Œufs : ramassage par lot et par jour, taux de ponte, hausses et baisses, valeur des œufs.

Le taux de ponte = œufs pondus (bons + cassés) ÷ poules présentes ce jour-là × 100.
Une baisse forte par rapport à la semaine d'avant est signalée dans « Écarts & contrôles ».
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import now_utc, query, transaction
from .discussion import notify
from .security import EDIT, VIEW, can, can_correct, require
from .stock import parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("oeufs", __name__, url_prefix="/oeufs")

TRAY = 30            # œufs par plateau
DROP_POINTS = 10     # baisse signalée : 10 points de % sous la moyenne des 7 jours précédents


def _d(value):
    return Date.fromisoformat(value)


def _label(d):
    return f"{d[8:10]}/{d[5:7]}"


def birds_on(lot, day):
    """Poules présentes ce jour-là (départ + mouvements jusqu'à ce jour)."""
    return lot["initial_count"] + query(
        "SELECT COALESCE(SUM(quantity), 0) AS q FROM lot_events WHERE lot_id = ? AND date <= ?", (lot["id"], day),
        one=True)["q"]


def collections(lot_id, since=None, until=None):
    sql, params = "SELECT * FROM egg_collections WHERE lot_id = ?", [lot_id]
    if since:
        sql += " AND date >= ?"
        params.append(since)
    if until:
        sql += " AND date <= ?"
        params.append(until)
    return query(sql + " ORDER BY date", params)


def daily(lot, since=None, until=None):
    """Une ligne par jour de ramassage : œufs, poules, taux."""
    rows = []
    for c in collections(lot["id"], since, until):
        laid = (c["good"] or 0) + (c["broken"] or 0)
        hens = birds_on(lot, c["date"])
        rows.append({"id": c["id"], "date": c["date"], "good": c["good"] or 0, "broken": c["broken"] or 0, "laid": laid,
                     "hens": hens, "rate": (laid / hens * 100) if hens > 0 else 0, "notes": c["notes"] or "",
                     "created_by": c["created_by"], "created_at": c["created_at"]})
    return rows


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def lot_egg_stats(lot):
    """Chiffres clés de ponte d'un lot (depuis le début)."""
    rows = daily(lot)
    total_good = sum(r["good"] for r in rows)
    total_laid = sum(r["laid"] for r in rows)
    broken = sum(r["broken"] for r in rows)
    end = _d(today())
    last7 = [r["rate"] for r in rows if (end - _d(r["date"])).days < 7]
    prev7 = [r["rate"] for r in rows if 7 <= (end - _d(r["date"])).days < 14]
    a7, p7 = _avg(last7), _avg(prev7)
    peak = max(rows, key=lambda r: r["rate"]) if rows else None
    trough = min(rows, key=lambda r: r["rate"]) if rows else None
    last = rows[-1] if rows else None
    return {
        "rows": rows, "days": len(rows), "good": total_good, "laid": total_laid, "broken": broken,
        "broken_pct": (broken / total_laid * 100) if total_laid else 0,
        "avg_rate": _avg([r["rate"] for r in rows]), "avg7": a7, "prev7": p7,
        "trend": (a7 - p7) if (a7 is not None and p7 is not None) else None,
        "peak": peak, "trough": trough, "last": last,
        "value": total_good * (lot["egg_price"] or 0),
        "per_hen": total_laid / lot["initial_count"] if lot["initial_count"] else 0,
    }


def lot_chart(lot, days=60):
    """Graphique du lot : œufs par jour (barres) + taux de ponte (courbe), avec le repère du début de ponte."""
    from .lots import date_of_week

    rows = daily(lot)
    if not rows:
        return None
    first = max(_d(rows[0]["date"]), _d(today()) - timedelta(days=days - 1))
    by = {r["date"]: r for r in rows}
    labels, bars, line, tips = [], [], [], []
    d = first
    while d <= _d(today()):
        key = d.isoformat()
        r = by.get(key)
        labels.append(_label(key))
        bars.append(r["laid"] if r else 0)
        line.append(round(r["rate"], 1) if r else None)
        tips.append(f"{r['hens']} poules · {r['broken']} cassé(s)" if r else "pas de ramassage saisi")
        d += timedelta(days=1)
    lay = date_of_week(lot, lot["laying_week"] or 18).isoformat()
    marks = [{"at": _label(lay), "text": f"🥚 Début de ponte prévu (S{lot['laying_week'] or 18})"}] if _label(lay) in labels else []
    return {"labels": labels, "bars": bars, "line": line, "lineMax": 100, "lineUnit": "%", "unit": "œufs",
            "barLabel": "Œufs", "lineLabel": "Taux de ponte", "tips": tips, "marks": marks}


def farm_chart(lots, days=60):
    """Taux de ponte de chaque lot sur la même courbe + œufs de la ferme par jour."""
    from .lots import date_of_week

    end = _d(today())
    start = end - timedelta(days=days - 1)
    keys = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    labels = [_label(k) for k in keys]
    series, totals, marks, first_used = [], [0] * days, [], None
    for lot in lots:
        rows = {r["date"]: r for r in daily(lot, since=keys[0])}
        if rows:
            idx = [i for i, k in enumerate(keys) if k in rows]
            first_used = min(idx[0], first_used if first_used is not None else idx[0])
        values = [round(rows[k]["rate"], 1) if k in rows else None for k in keys]
        extra = [f"{rows[k]['laid']} œufs" if k in rows else "" for k in keys]
        for i, k in enumerate(keys):
            if k in rows:
                totals[i] += rows[k]["laid"]
        lay = date_of_week(lot, lot["laying_week"] or 18).isoformat()
        if keys[0] <= lay <= keys[-1]:
            marks.append({"at": _label(lay), "text": f"🥚 {lot['name']}"})
        if rows:
            series.append({"name": lot["name"], "values": values, "extra": extra})
    if first_used is None:
        return None
    cut = first_used  # on commence au premier jour saisi : pas de jours vides au début
    for s in series:
        s["values"], s["extra"] = s["values"][cut:], s["extra"][cut:]
    labels, totals = labels[cut:], totals[cut:]
    marks = [m for m in marks if m["at"] in labels]
    filled = [(v, labels[i]) for i, v in enumerate(totals) if v > 0]
    summary = None
    if filled:
        hi, lo = max(filled), min(filled)
        summary = {"hi": hi[0], "hi_day": hi[1], "lo": lo[0], "lo_day": lo[1],
                   "avg": sum(v for v, _ in filled) / len(filled), "days": len(filled)}
    lots_sum = []
    for s in series:
        vals = [(v, labels[i]) for i, v in enumerate(s["values"]) if v is not None]
        if vals:
            lots_sum.append({"name": s["name"], "hi": max(vals), "lo": min(vals), "last": vals[-1]})
    return {"summary": summary, "lots_sum": lots_sum,
            "rates": {"labels": labels, "series": series, "unit": "%", "max": 100, "marks": marks},
            "eggs": {"labels": labels, "values": totals, "unit": "œufs", "color": "#c9971c"},
            "today": totals[-1], "month": sum(totals[-30:])}


def drop_alerts(since=None, until=None):
    """Jours où le taux de ponte d'un lot chute nettement sous sa moyenne des 7 jours précédents."""
    out = []
    for lot in query("SELECT * FROM lots"):
        rows = daily(lot)
        for i, r in enumerate(rows):
            prev = [p["rate"] for p in rows[max(0, i - 7):i] if (_d(r["date"]) - _d(p["date"])).days <= 7]
            if len(prev) < 3:
                continue
            avg = sum(prev) / len(prev)
            if avg >= 20 and r["rate"] < avg - DROP_POINTS:
                if (since and r["date"] < since) or (until and r["date"] > until):
                    continue
                lost = round((avg - r["rate"]) / 100 * r["hens"])
                out.append({
                    "domain": "oeufs", "date": r["date"], "name": lot["name"], "what": "Baisse de ponte",
                    "before": round(avg, 1), "after": round(r["rate"], 1), "qty": -lost, "unit": "œufs",
                    "value": -lost * (lot["egg_price"] or 0), "why": r["notes"], "pct": True,
                    "by": f"{r['laid']} œufs · {r['hens']} poules", "table": "egg_collections", "id": r["id"],
                    "url": url_for("lots.lot", lot_id=lot["id"]) + "#ponte", "module": "oeufs",
                })
    return out


# ---------------------------------------------------------------------------
@bp.route("/", methods=["GET", "POST"])
@require("oeufs", VIEW)
def index():
    """Ramassage du jour pour tous les lots + courbes de ponte."""
    from .lots import laying_info, lot_week

    date = request.values.get("date") or today()
    if not valid_date(date) or date > today():
        date = today()
    lots = query("SELECT * FROM lots WHERE status = 'actif' AND arrival_date <= ? ORDER BY name", (date,))
    if request.method == "POST":
        if not can("oeufs", EDIT):
            abort(403)
        saved, lines = 0, []
        rows = zip(request.form.getlist("lot_id"), request.form.getlist("trays"), request.form.getlist("eggs"),
                   request.form.getlist("broken"), request.form.getlist("notes"))
        with transaction() as conn:
            for lot_id, trays, eggs, broken, notes in rows:
                lot = next((l for l in lots if str(l["id"]) == lot_id), None)
                if lot is None or not any(v.strip() for v in (trays, eggs, broken)):
                    continue
                good = int(round((parse_num(trays, 0) or 0) * TRAY + (parse_num(eggs, 0) or 0)))
                bad = int(round(parse_num(broken, 0) or 0))
                if good < 0 or bad < 0:
                    flash(f"{lot['name']} : nombre d'œufs invalide.", "error")
                    continue
                notes = " ".join(notes.split())[:150]
                old = conn.execute("SELECT * FROM egg_collections WHERE lot_id = ? AND date = ?", (lot["id"], date)).fetchone()
                if old:
                    if old["good"] == good and old["broken"] == bad and (old["notes"] or "") == notes:
                        continue
                    conn.execute("UPDATE egg_collections SET good = ?, broken = ?, notes = ?, created_by = ?, created_at = ? "
                                 "WHERE id = ?", (good, bad, notes, g.user["id"], now_utc(), old["id"]))
                else:
                    conn.execute("INSERT INTO egg_collections (lot_id, date, good, broken, notes, created_by, created_at) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (lot["id"], date, good, bad, notes, g.user["id"], now_utc()))
                hens = birds_on(lot, date)
                rate = (good + bad) / hens * 100 if hens else 0
                lines.append(f"{lot['name']} {good + bad} œufs ({rate:.0f} %)" + (" — corrigé" if old else "")
                             + (f" « {notes} »" if notes else ""))
                saved += 1
        if saved:
            log_activity("Ramassage d'œufs", f"{date_fr(date)} : " + ", ".join(lines))
            notify("", None, f"🥚 Ramassage du {date_fr(date)} : " + " · ".join(lines))
            flash(f"Ramassage enregistré ({saved} lot(s)). Vos associés sont prévenus.", "success")
        else:
            flash("Rien de nouveau à enregistrer.", "info")
        return redirect(url_for("oeufs.index", date=date))

    rows = []
    for lot in lots:
        c = query("SELECT * FROM egg_collections WHERE lot_id = ? AND date = ?", (lot["id"], date), one=True)
        st = lot_egg_stats(lot)
        prev = [r for r in st["rows"] if r["date"] < date][-7:]
        rows.append({"lot": lot, "hens": birds_on(lot, date), "week": lot_week(lot, date), "c": c,
                     "laying": laying_info(lot, lot_week(lot, date)), "st": st,
                     "avg_before": _avg([r["rate"] for r in prev])})
    all_lots = query("SELECT * FROM lots WHERE status = 'actif' ORDER BY arrival_date")
    chart = farm_chart(all_lots)
    stats = [{"lot": lot, "st": lot_egg_stats(lot)} for lot in all_lots]
    return render_template("oeufs/index.html", rows=rows, date=date, tray=TRAY, chart=chart,
                           stats=[s for s in stats if s["st"]["days"]], is_today=date == today(), today_iso=today())


@bp.route("/<int:row_id>/supprimer", methods=["POST"])
@require("oeufs", EDIT)
def delete(row_id):
    row = query("SELECT e.*, l.name AS lot FROM egg_collections e JOIN lots l ON l.id = e.lot_id WHERE e.id = ?",
                (row_id,), one=True)
    if row is None:
        abort(404)
    if not can_correct("oeufs", row):
        abort(403)
    from .db import execute

    execute("DELETE FROM egg_collections WHERE id = ?", (row_id,))
    log_activity("Ramassage annulé", f"{row['lot']} {date_fr(row['date'])} : {row['good'] + row['broken']} œufs")
    notify("lot", row["lot_id"], f"↩️ Ramassage du {date_fr(row['date'])} annulé pour « {row['lot']} » "
           f"({row['good'] + row['broken']} œufs).")
    flash("Ramassage annulé.", "success")
    back = request.form.get("next") or url_for("oeufs.index", date=row["date"])
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("oeufs.index"))


__all__ = ["bp", "lot_egg_stats", "lot_chart", "farm_chart", "drop_alerts", "fmt_money"]
