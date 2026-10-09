"""Lots de poules : arrivée, âge, effectif, morts, provende donnée et prévision jusqu'à la réforme.

Règle du logiciel : il PROPOSE (provende et quantité du programme) mais l'utilisateur décide
toujours. Le stock de provende ne peut pas devenir négatif : on l'explique au lieu de bloquer.
"""
import json
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, can_correct, require
from .stock import EPS, accounts, feed_stock, parse_num, recompute_formula, today, valid_date
from .utils import date_fr, fmt_money, fmt_qty, log_activity

bp = Blueprint("lots", __name__, url_prefix="/lots")

EVENT_KINDS = {
    "mort": ("Mortalité", -1),
    "reforme": ("Réforme / vente de poules", -1),
    "perte": ("Disparition / vol", -1),
    "ajout": ("Ajout de poules", 1),
    "comptage": ("Correction après comptage", 0),  # le signe vient de la différence
}


# ---------------------------------------------------------------------------
# Calculs
# ---------------------------------------------------------------------------
def _d(value):
    return Date.fromisoformat(value)


def lot_age_days(lot, on=None):
    on = _d(on or today())
    return (on - _d(lot["arrival_date"])).days + (lot["age_days_at_arrival"] or 0)


def lot_week(lot, on=None):
    return max(1, lot_age_days(lot, on) // 7 + 1)


def date_of_week(lot, week):
    """Premier jour de la semaine d'âge `week` du lot."""
    return _d(lot["arrival_date"]) + timedelta(days=(week - 1) * 7 - (lot["age_days_at_arrival"] or 0))


def effectif(lot_id, on=None):
    row = query("SELECT initial_count FROM lots WHERE id = ?", (lot_id,), one=True)
    if row is None:
        return 0
    sql = "SELECT COALESCE(SUM(quantity), 0) AS q FROM lot_events WHERE lot_id = ?"
    params = [lot_id]
    if on:
        sql += " AND date <= ?"
        params.append(on)
    return row["initial_count"] + query(sql, params, one=True)["q"]


def program_rows(program_id):
    if not program_id:
        return []
    return query("""SELECT w.*, f.name AS formula, f.avg_cost FROM feed_program_weeks w
                    LEFT JOIN formulas f ON f.id = w.formula_id
                    WHERE w.program_id = ? ORDER BY w.week_from""", (program_id,))


def program_for_week(rows, week):
    for r in rows:
        if r["week_from"] <= week <= (r["week_to"] or r["week_from"]):
            return r
    return None


def suggestion(lot, on=None):
    """Ce que le programme propose pour ce lot aujourd'hui (provende et kg)."""
    week = lot_week(lot, on)
    birds = effectif(lot["id"], on)
    row = program_for_week(program_rows(lot["program_id"]), week)
    if row is None:
        return {"week": week, "birds": birds, "formula_id": None, "formula": None, "grams": 0, "kg": 0}
    kg = birds * (row["grams_per_bird"] or 0) / 1000
    return {"week": week, "birds": birds, "formula_id": row["formula_id"], "formula": row["formula"],
            "grams": row["grams_per_bird"] or 0, "kg": kg}


def consumption(lot_id):
    rows = query("""SELECT fd.*, f.name AS formula FROM feedings fd LEFT JOIN formulas f ON f.id = fd.formula_id
                    WHERE fd.lot_id = ? ORDER BY fd.date, fd.id""", (lot_id,))
    total = sum(r["quantity"] for r in rows)
    cost = sum(r["quantity"] * r["unit_cost"] for r in rows)
    by_formula = {}
    for r in rows:
        item = by_formula.setdefault(r["formula"] or "?", {"kg": 0, "cost": 0})
        item["kg"] += r["quantity"]
        item["cost"] += r["quantity"] * r["unit_cost"]
    return {"rows": rows, "kg": total, "cost": cost, "by_formula": by_formula}


def forecast(lot):
    """Provende à venir, semaine par semaine, jusqu'à la semaine de réforme prévue."""
    rows = program_rows(lot["program_id"])
    start = lot_week(lot)
    end = max(start, lot["reform_week"] or 72)
    birds = effectif(lot["id"])
    # mortalité hebdomadaire observée (plafonnée) pour diminuer l'effectif dans la prévision
    deaths = -query("SELECT COALESCE(SUM(quantity), 0) AS q FROM lot_events WHERE lot_id = ? AND kind IN ('mort', 'perte')",
                    (lot["id"],), one=True)["q"]
    weeks_lived = max(1, start - 1 - (lot["age_days_at_arrival"] or 0) // 7)
    weekly_rate = min(0.02, (deaths / max(1, lot["initial_count"])) / weeks_lived) if deaths > 0 else 0
    weeks, total_kg, total_cost, total_days, by_formula = [], 0.0, 0.0, 0, {}
    before_kg = before_cost = 0.0
    last_grams = 0
    for w in range(start, end + 1):
        row = program_for_week(rows, w)
        grams = (row["grams_per_bird"] if row else 0) or last_grams
        last_grams = grams
        days = 7
        first_day = date_of_week(lot, w)
        if w == start:  # semaine en cours : seulement les jours restants (aujourd'hui compris)
            days = 7 - (lot_age_days(lot) % 7)
            first_day = _d(today())
        kg = birds * grams * days / 1000
        price = (row["avg_cost"] if row else 0) or 0
        cost = kg * price
        name = row["formula"] if row else "—"
        if w < (lot["laying_week"] or 18):
            before_kg += kg
            before_cost += cost
        weeks.append({"week": w, "birds": round(birds), "grams": grams, "kg": kg, "cost": cost, "days": days,
                      "start": first_day.isoformat(), "formula": name})
        total_kg += kg
        total_cost += cost
        total_days += days
        item = by_formula.setdefault(name, {"kg": 0, "cost": 0, "from": w, "to": w, "days": 0, "grams": grams,
                                            "kg_day": birds * grams / 1000, "birds": round(birds)})
        item["kg"] += kg
        item["cost"] += cost
        item["days"] += days
        item["to"] = w
        birds *= (1 - weekly_rate)
    for item in by_formula.values():  # quantité par jour moyenne sur la période (mortalité comprise)
        item["kg_day_avg"] = item["kg"] / item["days"] if item["days"] else 0
    return {"weeks": weeks, "kg": total_kg, "cost": total_cost, "by_formula": by_formula, "days": total_days,
            "before_kg": before_kg, "before_cost": before_cost,
            "kg_day": weeks[0]["kg"] / weeks[0]["days"] if weeks and weeks[0]["days"] else 0,
            "end_date": date_of_week(lot, end + 1).isoformat(), "end_week": end, "weekly_rate": weekly_rate,
            "has_program": bool(rows)}


def weekly_series(lot, cons, fc):
    """Données du graphique : kg mangés par semaine d'âge + prévision."""
    past = {}
    for r in cons["rows"]:
        w = lot_week(lot, r["date"])
        past[w] = past.get(w, 0) + r["quantity"]
    first = min(list(past) + [lot_week(lot)]) if past else lot_week(lot)
    first = min(first, max(1, (lot["age_days_at_arrival"] or 0) // 7 + 1))
    last = fc["end_week"]
    future = {w["week"]: w["kg"] for w in fc["weeks"]}
    labels, eaten, planned = [], [], []
    for w in range(first, last + 1):
        labels.append(f"S{w}")
        eaten.append(round(past.get(w, 0), 1) if w <= lot_week(lot) else None)
        planned.append(round(future[w], 1) if w in future else None)
    return {"labels": labels, "bars": eaten, "line": planned, "now": f"S{lot_week(lot)}", "unit": "kg",
            "barLabel": "Mangé", "lineLabel": "Prévision"}


def laying_info(lot, week=None):
    """Où en est le lot par rapport au début de la ponte (semaine variable, choisie pour chaque lot)."""
    week = week or lot_week(lot)
    lw = lot["laying_week"] or 18
    reform = max(lw + 1, lot["reform_week"] or 72)
    start = date_of_week(lot, lw)
    info = {"week": lw, "date": start.isoformat(), "reform": reform,
            "pos": min(100, max(0, (lw - 1) / reform * 100))}
    if week < lw:
        info.update(phase="élevage", label="Élevage", left=lw - week,
                    text=f"Ponte prévue semaine {lw}, dans {lw - week} semaine(s)")
    else:
        info.update(phase="ponte", label="En ponte", since=week - lw + 1,
                    text=f"En ponte depuis la semaine {lw} ({week - lw + 1} semaine(s))")
    return info


def lot_summary(lot):
    birds = effectif(lot["id"])
    cons = consumption(lot["id"])
    deaths = -query("SELECT COALESCE(SUM(quantity), 0) AS q FROM lot_events WHERE lot_id = ? AND kind = 'mort'",
                    (lot["id"],), one=True)["q"]
    week = lot_week(lot)
    summary = {
        "birds": birds, "week": week, "days": lot_age_days(lot), "deaths": deaths,
        "mortality": (deaths / lot["initial_count"] * 100) if lot["initial_count"] else 0,
        "eaten_kg": cons["kg"], "eaten_cost": cons["cost"],
        "per_bird": cons["kg"] / lot["initial_count"] if lot["initial_count"] else 0,
        "progress": min(100, week / max(1, lot["reform_week"] or 72) * 100),
        "chick_cost": lot["initial_count"] * lot["chick_price"] + lot["other_costs"],
    }
    summary["laying"] = laying_info(lot, week)
    if lot["status"] == "actif":
        summary["suggest"] = suggestion(lot)
        summary["forecast"] = forecast(lot)
    return summary, cons


def _lot(lot_id):
    row = query("""SELECT l.*, p.name AS program, a.name AS account FROM lots l
                   LEFT JOIN feed_programs p ON p.id = l.program_id LEFT JOIN accounts a ON a.id = l.account_id
                   WHERE l.id = ?""", (lot_id,), one=True)
    if row is None:
        abort(404)
    return row


def _programs():
    return query("SELECT * FROM feed_programs WHERE active = 1 ORDER BY name")


def _formulas():
    return query("SELECT * FROM formulas WHERE active = 1 ORDER BY name")


# ---------------------------------------------------------------------------
# Liste et fiche
# ---------------------------------------------------------------------------
@bp.route("/")
@require("lots", VIEW)
def index():
    show = request.args.get("voir", "actifs")
    rows = query(f"""SELECT l.*, p.name AS program FROM lots l LEFT JOIN feed_programs p ON p.id = l.program_id
                     {"WHERE l.status = 'actif'" if show == 'actifs' else ''} ORDER BY l.arrival_date DESC, l.id DESC""")
    items = []
    for lot in rows:
        summary, _cons = lot_summary(lot)
        items.append({"lot": lot, "s": summary})
    totals = {
        "lots": sum(1 for i in items if i["lot"]["status"] == "actif"),
        "birds": sum(i["s"]["birds"] for i in items if i["lot"]["status"] == "actif"),
        "today_kg": sum(i["s"].get("suggest", {}).get("kg", 0) for i in items),
        "eaten_cost": sum(i["s"]["eaten_cost"] for i in items),
    }
    closed = query("SELECT COUNT(*) AS n FROM lots WHERE status != 'actif'", one=True)["n"]
    return render_template("lots/index.html", items=items, totals=totals, show=show, closed=closed)


@bp.route("/<int:lot_id>")
@require("lots", VIEW)
def lot(lot_id):
    item = _lot(lot_id)
    summary, cons = lot_summary(item)
    events = query("""SELECT e.*, u.username FROM lot_events e LEFT JOIN users u ON u.id = e.created_by
                      WHERE e.lot_id = ? ORDER BY e.date DESC, e.id DESC""", (lot_id,))
    feedings = query("""SELECT fd.*, f.name AS formula, u.username FROM feedings fd
                        LEFT JOIN formulas f ON f.id = fd.formula_id LEFT JOIN users u ON u.id = fd.created_by
                        WHERE fd.lot_id = ? ORDER BY fd.date DESC, fd.id DESC LIMIT 120""", (lot_id,))
    chart = weekly_series(item, cons, summary["forecast"]) if "forecast" in summary else None
    return render_template("lots/lot.html", item=item, s=summary, cons=cons, events=events, feedings=feedings,
                           kinds=EVENT_KINDS, chart=json.dumps(chart) if chart else None,
                           formulas=_formulas(), today=today(), programs=program_rows(item["program_id"]))


# ---------------------------------------------------------------------------
# Création / modification
# ---------------------------------------------------------------------------
def _form_values(existing=None):
    f = request.form
    return {
        "name": " ".join(f.get("name", "").split())[:60],
        "arrival_date": f.get("arrival_date", "").strip(),
        "age_days_at_arrival": int(parse_num(f.get("age_weeks"), 0) * 7 + parse_num(f.get("age_days"), 0)),
        "initial_count": int(parse_num(f.get("initial_count"), 0) or 0),
        "breed": " ".join(f.get("breed", "").split())[:60],
        "building": " ".join(f.get("building", "").split())[:60],
        "supplier": " ".join(f.get("supplier", "").split())[:80],
        "chick_price": parse_num(f.get("chick_price"), 0) or 0,
        "other_costs": parse_num(f.get("other_costs"), 0) or 0,
        "paid": parse_num(f.get("paid")),
        "account_id": f.get("account_id") or None,
        "program_id": f.get("program_id") or None,
        "reform_week": int(parse_num(f.get("reform_week"), 72) or 72),
        "laying_week": int(parse_num(f.get("laying_week"), 18) or 18),
        "egg_price": parse_num(f.get("egg_price"), 0) or 0,
        "notes": f.get("notes", "").strip(),
    }


def _errors(v, lot_id=None):
    errors = []
    if len(v["name"]) < 1:
        errors.append("Donnez un nom au lot (ex. « Lot A » ou « Bande octobre 2026 »).")
    elif query("SELECT 1 FROM lots WHERE name = ? COLLATE NOCASE AND id != ?", (v["name"], lot_id or 0), one=True):
        errors.append("Un lot porte déjà ce nom.")
    if not valid_date(v["arrival_date"]):
        errors.append("Date d'arrivée invalide.")
    if v["initial_count"] <= 0:
        errors.append("Indiquez le nombre de poules (ou poussins) à l'arrivée.")
    if v["chick_price"] < 0 or v["other_costs"] < 0:
        errors.append("Les prix ne peuvent pas être négatifs.")
    if v["reform_week"] < 1:
        errors.append("La semaine de réforme doit être supérieure à 0.")
    if v["laying_week"] < 1 or v["laying_week"] >= v["reform_week"]:
        errors.append("La semaine de début de ponte doit être avant la semaine de réforme.")
    return errors


@bp.route("/nouveau", methods=["GET", "POST"])
@require("lots", EDIT)
def lot_new():
    count = query("SELECT COUNT(*) AS n FROM lots", one=True)["n"]
    values = {"name": f"Lot {chr(65 + count % 26)}", "arrival_date": today(), "age_days_at_arrival": 0,
              "initial_count": "", "breed": "", "building": "", "supplier": "", "chick_price": "", "other_costs": "",
              "paid": None, "account_id": None, "program_id": None, "reform_week": 72, "laying_week": 18, "egg_price": "", "notes": ""}
    # le logiciel propose les réglages du dernier lot (tout reste modifiable)
    last = query("SELECT * FROM lots ORDER BY id DESC LIMIT 1", one=True)
    if last:
        for key in ("breed", "building", "supplier", "chick_price", "reform_week", "laying_week", "egg_price"):
            v = last[key]
            if v not in (None, "", 0):
                values[key] = int(v) if isinstance(v, float) and v.is_integer() else v
        if last["program_id"] and query("SELECT 1 FROM feed_programs WHERE id = ? AND active = 1", (last["program_id"],), one=True):
            values["program_id"] = last["program_id"]
    if not values["program_id"]:
        progs = query("SELECT id FROM feed_programs WHERE active = 1")
        if len(progs) == 1:
            values["program_id"] = progs[0]["id"]
    if request.method == "POST":
        values = _form_values()
        errors = _errors(values)
        total = values["initial_count"] * values["chick_price"] + values["other_costs"]
        paid = total if values["paid"] is None else values["paid"]
        if paid < 0 or paid > total + 0.5:
            errors.append("Le montant payé doit être entre 0 et le coût total.")
        account = query("SELECT * FROM accounts WHERE id = ?", (values["account_id"] or 0,), one=True)
        if paid > 0 and account is None:
            errors.append("Indiquez d'où vient l'argent (caisse ou propriétaire), ou mettez 0 dans « payé ».")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            with transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO lots (name, arrival_date, age_days_at_arrival, initial_count, breed, building, supplier,
                       chick_price, other_costs, paid, account_id, program_id, reform_week, egg_price, notes,
                       created_by, created_at, laying_week) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (values["name"], values["arrival_date"], values["age_days_at_arrival"], values["initial_count"],
                     values["breed"], values["building"], values["supplier"], values["chick_price"],
                     values["other_costs"], paid, account["id"] if account and paid > 0 else None,
                     values["program_id"], values["reform_week"], values["egg_price"], values["notes"],
                     g.user["id"], now_utc(), values["laying_week"]))
                lot_id = cur.lastrowid
                if paid > 0:
                    conn.execute(
                        """INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                           VALUES (?, ?, ?, 'achat_poussins', ?, 'lot', ?, ?, ?)""",
                        (values["arrival_date"], account["id"], -paid, f"Achat poussins {values['name']}", lot_id,
                         g.user["id"], now_utc()))
            log_activity("Lot créé", f"{values['name']} — {values['initial_count']} poules")
            notify("lot", lot_id, f"🐣 Nouveau lot « {values['name']} » : {values['initial_count']} "
                   f"{'poussins' if values['age_days_at_arrival'] < 7 else 'poules'} arrivés le {date_fr(values['arrival_date'])}"
                   + (f" ({fmt_money(total)})" if total else "") + ".")
            flash(f"Lot « {values['name']} » enregistré.", "success")
            return redirect(url_for("lots.lot", lot_id=lot_id))
    return render_template("lots/lot_form.html", values=values, lot=None, programs=_programs(),
                           accounts=accounts(), suggestions=_suggestions())


def _suggestions():
    return {k: [r["v"] for r in query(f"SELECT DISTINCT {k} AS v FROM lots WHERE {k} != '' ORDER BY {k}")]
            for k in ("breed", "building", "supplier")}


@bp.route("/<int:lot_id>/modifier", methods=["GET", "POST"])
@require("lots", EDIT)
def lot_edit(lot_id):
    item = _lot(lot_id)
    values = dict(item)
    if request.method == "POST":
        values = _form_values()
        errors = _errors(values, lot_id)
        if values["initial_count"] + (effectif(lot_id) - item["initial_count"]) < 0:
            errors.append("Avec ce nombre de départ, l'effectif deviendrait négatif.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            changes = []
            labels = {"name": "nom", "arrival_date": "arrivée", "initial_count": "nombre au départ",
                      "program_id": "programme", "laying_week": "début de ponte (semaine)",
                      "reform_week": "semaine de réforme", "chick_price": "prix du poussin"}
            for key, label in labels.items():
                if str(values[key] or "") != str(item[key] or ""):
                    changes.append(f"{label} {item[key] or '—'} → {values[key] or '—'}")
            execute("""UPDATE lots SET name = ?, arrival_date = ?, age_days_at_arrival = ?, initial_count = ?, breed = ?,
                       building = ?, supplier = ?, chick_price = ?, other_costs = ?, program_id = ?, reform_week = ?,
                       egg_price = ?, notes = ?, laying_week = ? WHERE id = ?""",
                    (values["name"], values["arrival_date"], values["age_days_at_arrival"], values["initial_count"],
                     values["breed"], values["building"], values["supplier"], values["chick_price"],
                     values["other_costs"], values["program_id"], values["reform_week"], values["egg_price"],
                     values["notes"], values["laying_week"], lot_id))
            log_activity("Lot modifié", f"{values['name']} : " + ("; ".join(changes) or "détails"))
            if changes:
                notify("lot", lot_id, f"✏️ Lot « {values['name']} » modifié : " + "; ".join(changes))
            flash("Lot mis à jour.", "success")
            return redirect(url_for("lots.lot", lot_id=lot_id))
    return render_template("lots/lot_form.html", values=values, lot=item, programs=_programs(),
                           accounts=accounts(), suggestions=_suggestions())


@bp.route("/<int:lot_id>/terminer", methods=["POST"])
@require("lots", MANAGE)
def lot_close(lot_id):
    item = _lot(lot_id)
    if item["status"] == "actif":
        end = request.form.get("end_date") or today()
        execute("UPDATE lots SET status = 'termine', end_date = ? WHERE id = ?", (end, lot_id))
        log_activity("Lot terminé", item["name"])
        notify("lot", lot_id, f"🏁 Lot « {item['name']} » terminé le {date_fr(end)}.")
        flash("Lot terminé. Il reste consultable dans « Tous les lots ».", "success")
    else:
        execute("UPDATE lots SET status = 'actif', end_date = '' WHERE id = ?", (lot_id,))
        log_activity("Lot réactivé", item["name"])
        flash("Lot réactivé.", "success")
    return redirect(url_for("lots.lot", lot_id=lot_id))


@bp.route("/<int:lot_id>/supprimer", methods=["POST"])
@require("lots", MANAGE)
def lot_delete(lot_id):
    item = _lot(lot_id)
    if query("SELECT 1 FROM feedings WHERE lot_id = ? LIMIT 1", (lot_id,), one=True):
        flash("Ce lot a déjà reçu de la provende : annulez d'abord ces distributions, ou terminez le lot.", "error")
        return redirect(url_for("lots.lot", lot_id=lot_id))
    with transaction() as conn:
        conn.execute("DELETE FROM lot_events WHERE lot_id = ?", (lot_id,))
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'lot' AND ref_id = ?", (lot_id,))
        conn.execute("DELETE FROM lots WHERE id = ?", (lot_id,))
    log_activity("Lot supprimé", item["name"])
    notify("", None, f"↩️ Lot « {item['name']} » supprimé (saisie annulée).")
    flash("Lot supprimé.", "success")
    return redirect(url_for("lots.index"))


# ---------------------------------------------------------------------------
# Effectif : morts, réformes, ajouts, comptage
# ---------------------------------------------------------------------------
@bp.route("/<int:lot_id>/effectif", methods=["POST"])
@require("lots", EDIT)
def event_new(lot_id):
    item = _lot(lot_id)
    kind = request.form.get("kind", "mort")
    qty = parse_num(request.form.get("quantity"))
    date = request.form.get("date") or today()
    notes = request.form.get("notes", "").strip()[:150]
    back = redirect(url_for("lots.lot", lot_id=lot_id) + "#effectif")
    if kind not in EVENT_KINDS or qty is None or qty < 0 or (kind != "comptage" and qty <= 0) or not valid_date(date):
        flash("Indiquez un nombre valide et une date.", "error")
        return back
    current = effectif(lot_id)
    if kind == "comptage":
        signed = int(round(qty)) - current
        if signed == 0:
            flash(f"Aucun écart : le logiciel compte déjà {current} poules.", "info")
            return back
        notes = notes  # l'explication de l'écart (le comptage avant → après est recalculé à l'affichage)
    else:
        signed = int(round(qty)) * EVENT_KINDS[kind][1]
    if current + signed < 0:
        flash(f"Impossible : il n'y a que {current} poules dans ce lot.", "error")
        return back
    execute("INSERT INTO lot_events (lot_id, date, kind, quantity, notes, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lot_id, date, kind, signed, notes, g.user["id"], now_utc()))
    label = EVENT_KINDS[kind][0]
    log_activity(f"Lot — {label}", f"{item['name']} : {signed:+d}")
    icon = {"mort": "🪦", "reforme": "🏷️", "perte": "⚠️", "ajout": "➕", "comptage": "📋"}[kind]
    notify("lot", lot_id, f"{icon} {label} — lot « {item['name']} » : {signed:+d} poule(s) le {date_fr(date)}. "
           f"Effectif : {current} → {current + signed}." + (f" ({notes})" if notes else ""))
    flash(f"{label} enregistrée : {signed:+d}. Effectif : {current + signed} poules.", "success")
    return back


@bp.route("/effectif/<int:event_id>/supprimer", methods=["POST"])
@require("lots", EDIT)
def event_delete(event_id):
    ev = query("SELECT * FROM lot_events WHERE id = ?", (event_id,), one=True)
    if ev is None:
        abort(404)
    if not can_correct("lots", ev):
        abort(403)
    if effectif(ev["lot_id"]) - ev["quantity"] < 0:
        flash("Impossible : l'effectif deviendrait négatif.", "error")
    else:
        execute("DELETE FROM lot_events WHERE id = ?", (event_id,))
        item = _lot(ev["lot_id"])
        log_activity("Lot — saisie d'effectif annulée", f"{item['name']} : {ev['quantity']:+d}")
        notify("lot", ev["lot_id"], f"↩️ Lot « {item['name']} » : saisie {EVENT_KINDS.get(ev['kind'], ('?',))[0].lower()} "
               f"{ev['quantity']:+d} du {date_fr(ev['date'])} annulée.")
        flash("Saisie annulée.", "success")
    return redirect(url_for("lots.lot", lot_id=ev["lot_id"]) + "#effectif")


# ---------------------------------------------------------------------------
# Alimentation : provende donnée aux lots
# ---------------------------------------------------------------------------
def _give_feed(conn, lot, formula, qty, date, notes):
    stock = feed_stock(formula["id"], conn)
    if qty > stock + EPS:
        return (f"Lot « {lot['name']} » : il ne reste que {fmt_qty(stock, 'kg')} de « {formula['name']} » "
                f"(demandé {fmt_qty(qty, 'kg')}). Fabriquez d'abord de la provende ou faites une entrée de stock.")
    birds = effectif(lot["id"], date)
    cur = conn.execute("""INSERT INTO feedings (lot_id, date, formula_id, quantity, unit_cost, birds, notes, created_by, created_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (lot["id"], date, formula["id"], qty, formula["avg_cost"], birds, notes, g.user["id"], now_utc()))
    conn.execute("""INSERT INTO feed_moves (date, formula_id, quantity, unit_cost, kind, notes, ref_type, ref_id, created_by, created_at)
                    VALUES (?, ?, ?, ?, 'distribution', ?, 'feeding', ?, ?, ?)""",
                 (date, formula["id"], -qty, formula["avg_cost"], f"Lot {lot['name']}", cur.lastrowid, g.user["id"], now_utc()))
    recompute_formula(conn, formula["id"])
    return None


@bp.route("/<int:lot_id>/provende", methods=["POST"])
@require("alimentation", EDIT)
def feeding_new(lot_id):
    item = _lot(lot_id)
    back = redirect(url_for("lots.lot", lot_id=lot_id) + "#provende")
    qty = parse_num(request.form.get("quantity"))
    date = request.form.get("date") or today()
    formula = query("SELECT * FROM formulas WHERE id = ?", (request.form.get("formula_id") or 0,), one=True)
    if formula is None or qty is None or qty <= 0 or not valid_date(date):
        flash("Choisissez la provende et indiquez une quantité en kg.", "error")
        return back
    with transaction() as conn:
        error = _give_feed(conn, item, formula, qty, date, request.form.get("notes", "").strip()[:150])
    if error:
        flash(error, "error")
        return back
    sug = suggestion(item, date)
    if sug["formula_id"] and sug["formula_id"] != formula["id"]:
        flash(f"Note : le programme prévoit « {sug['formula']} » cette semaine (semaine {sug['week']}). "
              "Votre choix a bien été enregistré.", "info")
    log_activity("Provende donnée", f"{item['name']} : {fmt_qty(qty, 'kg')} de {formula['name']}")
    birds = effectif(lot_id, date)
    flash(f"{fmt_qty(qty, 'kg')} de « {formula['name']} » donnés au lot « {item['name']} »"
          + (f" — {qty * 1000 / birds:.0f} g par poule" if birds else "") + ".", "success")
    return back


@bp.route("/alimentation", methods=["GET", "POST"])
@require("alimentation", VIEW)
def feeding_day():
    """Distribution du jour pour tous les lots : le logiciel propose, on corrige si besoin."""
    date = request.values.get("date") or today()
    if not valid_date(date):
        date = today()
    lots = query("SELECT * FROM lots WHERE status = 'actif' ORDER BY name")
    if request.method == "POST":
        if not can("alimentation", EDIT):
            abort(403)
        done, errors = [], []
        rows = zip(request.form.getlist("lot_id"), request.form.getlist("formula_id"), request.form.getlist("quantity"))
        with transaction() as conn:
            for lot_id, formula_id, q in rows:
                qty = parse_num(q)
                if not qty or qty <= 0:
                    continue
                lot = next((l for l in lots if str(l["id"]) == lot_id), None)
                formula = query("SELECT * FROM formulas WHERE id = ?", (formula_id or 0,), one=True)
                if lot is None or formula is None:
                    errors.append("Une ligne n'a pas de provende choisie : elle a été ignorée.")
                    continue
                error = _give_feed(conn, lot, formula, qty, date, "")
                if error:
                    errors.append(error)
                else:
                    done.append(f"{lot['name']} {fmt_qty(qty, 'kg')}")
        for message in errors:
            flash(message, "error")
        if done:
            log_activity("Distribution de provende", f"{date_fr(date)} : " + ", ".join(done))
            flash(f"Distribution enregistrée : {', '.join(done)}.", "success")
        return redirect(url_for("lots.feeding_day", date=date))
    rows = []
    for lot in lots:
        sug = suggestion(lot, date)
        given = query("SELECT COALESCE(SUM(quantity), 0) AS q FROM feedings WHERE lot_id = ? AND date = ?",
                      (lot["id"], date), one=True)["q"]
        rows.append({"lot": lot, "s": sug, "given": given})
    stocks = {f["id"]: feed_stock(f["id"]) for f in _formulas()}
    return render_template("lots/feeding_day.html", rows=rows, date=date, formulas=_formulas(), stocks=stocks)


@bp.route("/provende/<int:feeding_id>/supprimer", methods=["POST"])
@require("alimentation", EDIT)
def feeding_delete(feeding_id):
    fd = query("SELECT * FROM feedings WHERE id = ?", (feeding_id,), one=True)
    if fd is None:
        abort(404)
    if not can_correct("alimentation", fd):
        abort(403)
    with transaction() as conn:
        conn.execute("DELETE FROM feed_moves WHERE ref_type = 'feeding' AND ref_id = ?", (feeding_id,))
        conn.execute("DELETE FROM feedings WHERE id = ?", (feeding_id,))
        recompute_formula(conn, fd["formula_id"])
    item = _lot(fd["lot_id"])
    log_activity("Provende donnée annulée", f"{item['name']} : {fmt_qty(fd['quantity'], 'kg')}")
    notify("lot", fd["lot_id"], f"↩️ Lot « {item['name']} » : distribution de {fmt_qty(fd['quantity'], 'kg')} du "
           f"{date_fr(fd['date'])} annulée, la provende est revenue en stock.")
    flash("Distribution annulée : la provende est revenue en stock.", "success")
    return redirect(url_for("lots.lot", lot_id=fd["lot_id"]) + "#provende")


__all__ = ["bp", "MANAGE"]
