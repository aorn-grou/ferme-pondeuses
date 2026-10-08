"""Module Provenderie : formules, programmes par semaine d'âge, fabrication, stock de provende."""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .security import EDIT, MANAGE, VIEW, can_correct, require
from .stock import (
    moves_with_balance,
    EPS, feed_stock, formula_cost_per_kg, formula_lines, formulas_overview, material_stock, parse_num,
    production_needs, recompute_formula, recompute_material, today, valid_date,
)
from .units import convert, js_table, to_kg
from .utils import date_fr, fmt_money, fmt_qty, log_activity

from .discussion import notify  # noqa: E402

bp = Blueprint("provenderie", __name__, url_prefix="/provenderie")

PHASES = ["Démarrage", "Croissance", "Pré-ponte", "Ponte", "Ponte 2", "Autre"]
FEED_KINDS = {
    "fabrication": "Fabrication",
    "distribution": "Distribution aux lots",
    "perte": "Perte",
    "inventaire": "Correction d'inventaire",
    "entree": "Entrée (achat de provende / stock de départ)",
}


def _formula(formula_id):
    row = query("SELECT * FROM formulas WHERE id = ?", (formula_id,), one=True)
    if row is None:
        abort(404)
    return row


def _active_formulas():
    return query("SELECT * FROM formulas WHERE active = 1 ORDER BY name")


# ---------------------------------------------------------------------------
# Tableau du stock de provende
# ---------------------------------------------------------------------------
@bp.route("/")
@require("provenderie", VIEW)
def index():
    formulas = formulas_overview()
    month = today()[:7]
    month_prod = query("SELECT COALESCE(SUM(quantity), 0) AS q, COALESCE(SUM(cost_total), 0) AS c FROM productions "
                       "WHERE substr(date, 1, 7) = ?", (month,), one=True)
    recent = query("""SELECT p.*, f.name AS formula FROM productions p JOIN formulas f ON f.id = p.formula_id
                      ORDER BY p.date DESC, p.id DESC LIMIT 8""")
    summary = {
        "stock": sum(max(f["stock"], 0) for f in formulas),
        "value": sum(f["value"] for f in formulas),
        "month_qty": month_prod["q"],
        "month_cost": month_prod["c"],
        "alerts": sum(1 for f in formulas if f["status"] in ("low", "empty") and (f["alert_threshold"] or f["used_30"])),
    }
    programs = query("SELECT COUNT(*) AS n FROM feed_programs WHERE active = 1", one=True)["n"]
    return render_template("provenderie/index.html", formulas=formulas, summary=summary, recent=recent,
                           programs=programs, grams=_program_grams())


# ---------------------------------------------------------------------------
# Formules
# ---------------------------------------------------------------------------
@bp.route("/formules")
@require("provenderie", VIEW)
def formulas():
    rows = []
    for f in query("SELECT * FROM formulas ORDER BY active DESC, name"):
        item = dict(f)
        item["cost_per_kg"], item["total_qty"] = formula_cost_per_kg(f)
        item["nb_lines"] = len(formula_lines(f["id"]))
        rows.append(item)
    return render_template("provenderie/formulas.html", rows=rows)


def _formula_form():
    values = {
        "name": request.form.get("name", "").strip(),
        "phase": request.form.get("phase", "").strip(),
        "base_qty": parse_num(request.form.get("base_qty")),
        "alert_threshold": parse_num(request.form.get("alert_threshold"), 0) or 0,
        "notes": request.form.get("notes", "").strip(),
    }
    lines, parsed, errors = [], [], []
    seen = set()
    mids, qtys = request.form.getlist("material_id"), request.form.getlist("quantity")
    units = request.form.getlist("qty_unit") or [""] * len(mids)
    units += [""] * (len(mids) - len(units))
    for index, (mid, q, unit) in enumerate(zip(mids, qtys, units), 1):
        unit = " ".join(unit.split())[:15]
        lines.append({"material_id": mid, "quantity": q, "unit": unit})
        if not mid and not q:
            continue
        qty = parse_num(q)
        material = query("SELECT * FROM materials WHERE id = ? AND active = 1", (mid or 0,), one=True)
        if material is None:
            errors.append(f"Ligne {index} : choisissez une matière.")
            continue
        unit = unit or material["unit"]
        converted = convert(qty, unit, material["unit"]) if qty is not None else None
        if qty is None or qty <= 0:
            errors.append(f"Ligne {index} : la quantité doit être supérieure à 0.")
        elif converted is None:
            errors.append(f"« {material['name']} » est compté en « {material['unit']} » dans le stock : impossible de "
                          f"convertir des « {unit} ». Écrivez la quantité en {material['unit']} "
                          f"(ou changez l'unité de la matière dans sa fiche).")
        elif material["id"] in seen:
            errors.append(f"« {material['name']} » apparaît deux fois : regroupez les quantités sur une seule ligne.")
        else:
            seen.add(material["id"])
            parsed.append((material, converted, qty, unit))
    if len(values["name"]) < 2:
        errors.append("Donnez un nom à la formule (ex. « Ponte 1 »).")
    if not parsed:
        errors.append("Ajoutez au moins un ingrédient.")
    if values["base_qty"] is None:
        # poids total du mélange, toutes unités converties en kg
        values["base_qty"] = sum((to_kg(raw, unit) if to_kg(raw, unit) is not None else 0) for _, _, raw, unit in parsed)
    if values["base_qty"] <= 0:
        errors.append("La quantité de provende obtenue doit être supérieure à 0.")
    if values["alert_threshold"] < 0:
        errors.append("Le seuil d'alerte ne peut pas être négatif.")
    return values, lines, parsed, errors


def _lines_for_form(formula_id):
    return [{"material_id": str(l["material_id"]),
             "quantity": fmt_qty(l["input_qty"] if l["input_qty"] is not None else l["quantity"]),
             "unit": l["input_unit"] or l["unit"]} for l in formula_lines(formula_id)]


def _materials_for_form():
    return query("SELECT * FROM materials WHERE active = 1 ORDER BY name")


@bp.route("/formules/nouvelle", methods=["GET", "POST"])
@require("provenderie", EDIT)
def formula_new():
    values = {"name": "", "phase": "", "base_qty": "", "alert_threshold": 0, "notes": ""}
    lines = [{"material_id": "", "quantity": ""}]
    source_id = request.args.get("copie", type=int)
    if source_id and request.method == "GET":
        source = _formula(source_id)
        values = {"name": f"{source['name']} (copie)", "phase": source["phase"], "base_qty": source["base_qty"],
                  "alert_threshold": source["alert_threshold"], "notes": source["notes"]}
        lines = _lines_for_form(source_id)
    if request.method == "POST":
        values, lines, parsed, errors = _formula_form()
        if query("SELECT 1 FROM formulas WHERE name = ? COLLATE NOCASE AND active = 1", (values["name"],), one=True):
            errors.append("Une formule active porte déjà ce nom.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            with transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO formulas (name, phase, base_qty, alert_threshold, notes, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (values["name"], values["phase"], values["base_qty"], values["alert_threshold"], values["notes"], now_utc()),
                )
                formula_id = cur.lastrowid
                conn.executemany("INSERT INTO formula_lines (formula_id, material_id, quantity, input_qty, input_unit) "
                                 "VALUES (?, ?, ?, ?, ?)",
                                 [(formula_id, m["id"], q, raw, unit) for m, q, raw, unit in parsed])
            log_activity("Formule créée", values["name"])
            flash(f"Formule « {values['name']} » enregistrée. C'est la recette : pour que les stocks bougent, "
                  "cliquez maintenant sur « Fabriquer ».", "success")
            return redirect(url_for("provenderie.formula", formula_id=formula_id))
    return render_template("provenderie/formula_form.html", values=values, lines=lines, formula=None,
                           materials=_materials_for_form(), phases=PHASES, units_table=js_table())


@bp.route("/formules/<int:formula_id>")
@require("provenderie", VIEW)
def formula(formula_id):
    item = _formula(formula_id)
    lines = []
    for l in formula_lines(formula_id):
        line = dict(l)
        kg = to_kg(l["quantity"], l["unit"])
        line["kg"] = kg if kg is not None else l["quantity"]
        line["shown_qty"] = l["input_qty"] if l["input_qty"] is not None else l["quantity"]
        line["shown_unit"] = l["input_unit"] or l["unit"]
        line["cost"] = l["quantity"] * l["avg_cost"]
        lines.append(line)
    cost_per_kg, total_qty = formula_cost_per_kg(item)
    productions = query("SELECT * FROM productions WHERE formula_id = ? ORDER BY date DESC, id DESC LIMIT 20", (formula_id,))
    programs = query("""SELECT DISTINCT p.id, p.name FROM feed_program_weeks w JOIN feed_programs p ON p.id = w.program_id
                        WHERE w.formula_id = ? AND p.active = 1 ORDER BY p.name""", (formula_id,))
    base = item["base_qty"] or 100
    return render_template("provenderie/formula.html", item=item, lines=lines, cost_per_kg=cost_per_kg,
                           total_qty=total_qty, base=base, stock=feed_stock(formula_id), productions=productions,
                           programs=programs)


@bp.route("/formules/<int:formula_id>/modifier", methods=["GET", "POST"])
@require("provenderie", EDIT)
def formula_edit(formula_id):
    item = _formula(formula_id)
    values = dict(item)
    lines = _lines_for_form(formula_id)
    if request.method == "POST":
        values, lines, parsed, errors = _formula_form()
        if query("SELECT 1 FROM formulas WHERE name = ? COLLATE NOCASE AND active = 1 AND id != ?",
                 (values["name"], formula_id), one=True):
            errors.append("Une autre formule active porte déjà ce nom.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            with transaction() as conn:
                conn.execute("UPDATE formulas SET name = ?, phase = ?, base_qty = ?, alert_threshold = ?, notes = ? WHERE id = ?",
                             (values["name"], values["phase"], values["base_qty"], values["alert_threshold"],
                              values["notes"], formula_id))
                conn.execute("DELETE FROM formula_lines WHERE formula_id = ?", (formula_id,))
                conn.executemany("INSERT INTO formula_lines (formula_id, material_id, quantity, input_qty, input_unit) "
                                 "VALUES (?, ?, ?, ?, ?)",
                                 [(formula_id, m["id"], q, raw, unit) for m, q, raw, unit in parsed])
            log_activity("Formule modifiée", values["name"])
            flash("Formule mise à jour. Les fabrications déjà faites ne changent pas.", "success")
            return redirect(url_for("provenderie.formula", formula_id=formula_id))
    return render_template("provenderie/formula_form.html", values=values, lines=lines or [{"material_id": "", "quantity": ""}],
                           formula=item, materials=_materials_for_form(), phases=PHASES, units_table=js_table())


@bp.route("/formules/<int:formula_id>/archiver", methods=["POST"])
@require("provenderie", MANAGE)
def formula_archive(formula_id):
    item = _formula(formula_id)
    if request.form.get("action") == "delete":
        used = query("SELECT 1 FROM feed_moves WHERE formula_id = ? LIMIT 1", (formula_id,), one=True) or \
            query("SELECT 1 FROM feed_program_weeks WHERE formula_id = ? LIMIT 1", (formula_id,), one=True)
        if used:
            flash("Cette formule a un historique ou sert dans un programme : archivez-la plutôt.", "error")
            return redirect(url_for("provenderie.formula", formula_id=formula_id))
        with transaction() as conn:
            conn.execute("DELETE FROM formula_lines WHERE formula_id = ?", (formula_id,))
            conn.execute("DELETE FROM formulas WHERE id = ?", (formula_id,))
        log_activity("Formule supprimée", item["name"])
        flash(f"Formule « {item['name']} » supprimée.", "success")
        return redirect(url_for("provenderie.formulas"))
    new_state = 0 if item["active"] else 1
    if not new_state and query("""SELECT 1 FROM feed_program_weeks w JOIN feed_programs p ON p.id = w.program_id
                                  WHERE w.formula_id = ? AND p.active = 1 LIMIT 1""", (formula_id,), one=True):
        flash("Cette formule est utilisée dans un programme actif : changez d'abord le programme.", "error")
        return redirect(url_for("provenderie.formula", formula_id=formula_id))
    execute("UPDATE formulas SET active = ? WHERE id = ?", (new_state, formula_id))
    log_activity("Formule " + ("réactivée" if new_state else "archivée"), item["name"])
    flash("Formule " + ("réactivée." if new_state else "archivée."), "success")
    return redirect(url_for("provenderie.formula", formula_id=formula_id))


# ---------------------------------------------------------------------------
# Programmes d'alimentation par semaine d'âge
# ---------------------------------------------------------------------------
@bp.route("/programmes")
@require("provenderie", VIEW)
def programs():
    rows = query("""SELECT p.*, (SELECT COUNT(*) FROM feed_program_weeks w WHERE w.program_id = p.id) AS nb,
                           (SELECT MAX(week_to) FROM feed_program_weeks w WHERE w.program_id = p.id) AS last_week
                    FROM feed_programs p ORDER BY p.active DESC, p.name""")
    return render_template("provenderie/programs.html", rows=rows)


def _program_form():
    values = {"name": request.form.get("name", "").strip(), "notes": request.form.get("notes", "").strip()}
    rows, parsed, errors = [], [], []
    formulas = {str(f["id"]): f for f in _active_formulas()}
    for index, (wf, wt, fid, grams) in enumerate(zip(
            request.form.getlist("week_from"), request.form.getlist("week_to"),
            request.form.getlist("formula_id"), request.form.getlist("grams_per_bird")), 1):
        rows.append({"week_from": wf, "week_to": wt, "formula_id": fid, "grams_per_bird": grams})
        if not wf and not wt and not fid and not grams:
            continue
        start, end, gram = parse_num(wf), parse_num(wt), parse_num(grams, 0)
        if start is None or start < 1 or int(start) != start:
            errors.append(f"Ligne {index} : la semaine de début doit être un nombre entier (1, 2, 3…).")
            continue
        end = start if end is None else end
        if int(end) != end or end < start:
            errors.append(f"Ligne {index} : la semaine de fin doit être supérieure ou égale à la semaine de début.")
            continue
        if fid not in formulas:
            errors.append(f"Ligne {index} : choisissez une formule.")
            continue
        if gram is None or gram < 0:
            errors.append(f"Ligne {index} : quantité par poule invalide.")
            continue
        parsed.append((int(start), int(end), formulas[fid], gram))
    parsed.sort(key=lambda r: r[0])
    for previous, current in zip(parsed, parsed[1:]):
        if current[0] <= previous[1]:
            errors.append(f"Les semaines {previous[0]}–{previous[1]} et {current[0]}–{current[1]} se chevauchent.")
    if len(values["name"]) < 2:
        errors.append("Donnez un nom au programme (ex. « Programme pondeuses standard »).")
    if not parsed:
        errors.append("Ajoutez au moins une période (semaines + formule).")
    return values, rows, parsed, errors


def _save_program(conn, program_id, parsed):
    conn.execute("DELETE FROM feed_program_weeks WHERE program_id = ?", (program_id,))
    conn.executemany(
        "INSERT INTO feed_program_weeks (program_id, week_from, week_to, formula_id, grams_per_bird) VALUES (?, ?, ?, ?, ?)",
        [(program_id, a, b, f["id"], gr) for a, b, f, gr in parsed],
    )


def _program_rows(program_id):
    return [{"week_from": w["week_from"], "week_to": w["week_to"], "formula_id": str(w["formula_id"]),
             "grams_per_bird": fmt_qty(w["grams_per_bird"]) if w["grams_per_bird"] else ""}
            for w in query("SELECT * FROM feed_program_weeks WHERE program_id = ? ORDER BY week_from", (program_id,))]


@bp.route("/programmes/nouveau", methods=["GET", "POST"])
@require("provenderie", EDIT)
def program_new():
    values = {"name": "", "notes": ""}
    rows = [{"week_from": "1", "week_to": "", "formula_id": "", "grams_per_bird": ""}]
    source_id = request.args.get("copie", type=int)
    if source_id and request.method == "GET":
        source = query("SELECT * FROM feed_programs WHERE id = ?", (source_id,), one=True)
        if source:
            values = {"name": f"{source['name']} (copie)", "notes": source["notes"]}
            rows = _program_rows(source_id)
    if request.method == "POST":
        values, rows, parsed, errors = _program_form()
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            with transaction() as conn:
                cur = conn.execute("INSERT INTO feed_programs (name, notes, created_at) VALUES (?, ?, ?)",
                                   (values["name"], values["notes"], now_utc()))
                _save_program(conn, cur.lastrowid, parsed)
                program_id = cur.lastrowid
            log_activity("Programme d'alimentation créé", values["name"])
            flash(f"Programme « {values['name']} » enregistré.", "success")
            return redirect(url_for("provenderie.program", program_id=program_id))
    return render_template("provenderie/program_form.html", values=values, rows=rows, program=None,
                           formulas=_active_formulas())


@bp.route("/programmes/<int:program_id>")
@require("provenderie", VIEW)
def program(program_id):
    item = query("SELECT * FROM feed_programs WHERE id = ?", (program_id,), one=True)
    if item is None:
        abort(404)
    weeks = query("""SELECT w.*, f.name AS formula, f.phase FROM feed_program_weeks w JOIN formulas f ON f.id = w.formula_id
                     WHERE w.program_id = ? ORDER BY w.week_from""", (program_id,))
    return render_template("provenderie/program.html", item=item, weeks=weeks)


@bp.route("/programmes/<int:program_id>/modifier", methods=["GET", "POST"])
@require("provenderie", EDIT)
def program_edit(program_id):
    item = query("SELECT * FROM feed_programs WHERE id = ?", (program_id,), one=True)
    if item is None:
        abort(404)
    values = dict(item)
    rows = _program_rows(program_id)
    if request.method == "POST":
        values, rows, parsed, errors = _program_form()
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            with transaction() as conn:
                conn.execute("UPDATE feed_programs SET name = ?, notes = ? WHERE id = ?",
                             (values["name"], values["notes"], program_id))
                _save_program(conn, program_id, parsed)
            log_activity("Programme d'alimentation modifié", values["name"])
            flash("Programme mis à jour.", "success")
            return redirect(url_for("provenderie.program", program_id=program_id))
    return render_template("provenderie/program_form.html", values=values, rows=rows or [{"week_from": "1", "week_to": "",
                           "formula_id": "", "grams_per_bird": ""}], program=item, formulas=_active_formulas())


@bp.route("/programmes/<int:program_id>/archiver", methods=["POST"])
@require("provenderie", MANAGE)
def program_archive(program_id):
    item = query("SELECT * FROM feed_programs WHERE id = ?", (program_id,), one=True)
    if item is None:
        abort(404)
    if request.form.get("action") == "delete":
        with transaction() as conn:
            conn.execute("DELETE FROM feed_program_weeks WHERE program_id = ?", (program_id,))
            conn.execute("DELETE FROM feed_programs WHERE id = ?", (program_id,))
        log_activity("Programme d'alimentation supprimé", item["name"])
        flash("Programme supprimé.", "success")
        return redirect(url_for("provenderie.programs"))
    execute("UPDATE feed_programs SET active = ? WHERE id = ?", (0 if item["active"] else 1, program_id))
    log_activity("Programme " + ("archivé" if item["active"] else "réactivé"), item["name"])
    flash("Programme " + ("archivé." if item["active"] else "réactivé."), "success")
    return redirect(url_for("provenderie.program", program_id=program_id))


# ---------------------------------------------------------------------------
# Fabrication
# ---------------------------------------------------------------------------
@bp.route("/fabrication/nouvelle", methods=["GET", "POST"])
@require("provenderie", EDIT)
def production_new():
    formulas = [f for f in _active_formulas()]
    form = {"formula_id": request.args.get("formule", ""), "quantity": request.args.get("quantite", ""),
            "date": today(), "notes": ""}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        qty = parse_num(form["quantity"])
        formula = query("SELECT * FROM formulas WHERE id = ? AND active = 1", (form["formula_id"] or 0,), one=True)
        errors = []
        if formula is None:
            errors.append("Choisissez une formule.")
        if qty is None or qty <= 0:
            errors.append("Indiquez la quantité de provende à fabriquer (en kg).")
        if not valid_date(form["date"]):
            errors.append("Date invalide.")
        if not errors:
            with transaction() as conn:
                needs = production_needs(formula, qty, conn)
                if not needs:
                    errors.append("Cette formule n'a aucun ingrédient.")
                missing = [n for n in needs if n["missing"] > EPS]
                for n in missing:
                    errors.append(f"Stock insuffisant de « {n['name']} » : il faut {fmt_qty(n['need'], n['unit'])}, "
                                  f"il reste {fmt_qty(n['stock'], n['unit'])} (manque {fmt_qty(n['missing'], n['unit'])}).")
                if not errors:
                    cost_total = sum(n["cost"] for n in needs)
                    cur = conn.execute(
                        """INSERT INTO productions (date, formula_id, quantity, cost_total, cost_per_kg, notes, created_by, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (form["date"], formula["id"], qty, cost_total, cost_total / qty, form["notes"], g.user["id"], now_utc()),
                    )
                    production_id = cur.lastrowid
                    for n in needs:
                        conn.execute("INSERT INTO production_lines (production_id, material_id, quantity, unit_cost) VALUES (?, ?, ?, ?)",
                                     (production_id, n["material_id"], n["need"], n["unit_cost"]))
                        conn.execute(
                            """INSERT INTO stock_moves (date, material_id, quantity, unit_cost, kind, ref_type, ref_id, created_by, created_at)
                               VALUES (?, ?, ?, ?, 'fabrication', 'production', ?, ?, ?)""",
                            (form["date"], n["material_id"], -n["need"], n["unit_cost"], production_id, g.user["id"], now_utc()),
                        )
                        recompute_material(conn, n["material_id"])
                    conn.execute(
                        """INSERT INTO feed_moves (date, formula_id, quantity, unit_cost, kind, ref_type, ref_id, created_by, created_at)
                           VALUES (?, ?, ?, ?, 'fabrication', 'production', ?, ?, ?)""",
                        (form["date"], formula["id"], qty, cost_total / qty, production_id, g.user["id"], now_utc()),
                    )
                    recompute_formula(conn, formula["id"])
            if not errors:
                log_activity("Provende fabriquée", f"{formula['name']} : {fmt_qty(qty, 'kg')} — {fmt_money(cost_total)}")
                used = ", ".join(f"{n['name']} -{fmt_qty(n['need'], n['unit'])} (reste {fmt_qty(n['stock'] - n['need'], n['unit'])})"
                                 for n in needs)
                notify("production", production_id,
                       f"🏭 Fabrication : {fmt_qty(qty, 'kg')} de « {formula['name']} » ({fmt_money(cost_total / qty)} le kg). "
                       f"Matières retirées du stock : {used}.")
                flash(f"{fmt_qty(qty, 'kg')} de « {formula['name']} » fabriqués. Coût : {fmt_money(cost_total / qty)} le kg.",
                      "success")
                return redirect(url_for("provenderie.production", production_id=production_id))
        for message in errors:
            flash(message, "error")
    data = {}
    for f in formulas:
        data[f["id"]] = {"base": f["base_qty"] or 100, "lines": [
            {"name": l["name"], "unit": l["unit"], "qty": l["quantity"], "stock": l["stock"], "cost": l["avg_cost"]}
            for l in formula_lines(f["id"])]}
    return render_template("provenderie/production_form.html", form=form, formulas=formulas,
                           formula_data=json.dumps(data))


@bp.route("/fabrications")
@require("provenderie", VIEW)
def productions():
    start, end = request.args.get("du", ""), request.args.get("au", "")
    formula_id = request.args.get("formule", "")
    where, params = [], []
    if valid_date(start):
        where.append("p.date >= ?")
        params.append(start)
    if valid_date(end):
        where.append("p.date <= ?")
        params.append(end)
    if formula_id:
        where.append("p.formula_id = ?")
        params.append(formula_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = query(f"""SELECT p.*, f.name AS formula, u.username FROM productions p JOIN formulas f ON f.id = p.formula_id
                     LEFT JOIN users u ON u.id = p.created_by {clause} ORDER BY p.date DESC, p.id DESC LIMIT 300""", params)
    totals = {"qty": sum(r["quantity"] for r in rows), "cost": sum(r["cost_total"] for r in rows)}
    return render_template("provenderie/productions.html", rows=rows, totals=totals, start=start, end=end,
                           formula_id=formula_id, formulas=query("SELECT * FROM formulas ORDER BY name"))


@bp.route("/fabrications/<int:production_id>")
@require("provenderie", VIEW)
def production(production_id):
    item = query("""SELECT p.*, f.name AS formula, u.username FROM productions p JOIN formulas f ON f.id = p.formula_id
                    LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?""", (production_id,), one=True)
    if item is None:
        abort(404)
    lines = query("""SELECT l.*, m.name, m.unit FROM production_lines l JOIN materials m ON m.id = l.material_id
                     WHERE l.production_id = ? ORDER BY l.quantity DESC""", (production_id,))
    # avant → après pour chaque matière et pour la provende
    mat_moves = {m["item_id"]: m for m in moves_with_balance("material", ref=("production", production_id))}
    feed_moves = moves_with_balance("feed", ref=("production", production_id))
    return render_template("provenderie/production.html", item=item, lines=lines, mat_moves=mat_moves,
                           feed_move=feed_moves[0] if feed_moves else None)


@bp.route("/fabrications/<int:production_id>/corriger", methods=["POST"])
@require("provenderie", EDIT)
def production_edit(production_id):
    item = query("SELECT * FROM productions WHERE id = ?", (production_id,), one=True)
    if item is None:
        abort(404)
    if not can_correct("provenderie", item):
        abort(403)
    back = redirect(url_for("provenderie.production", production_id=production_id))
    qty = parse_num(request.form.get("quantity"))
    date = request.form.get("date", item["date"])
    notes = request.form.get("notes", item["notes"] or "").strip()
    if qty is None or qty <= 0 or not valid_date(date):
        flash("Quantité ou date invalide.", "error")
        return back
    if feed_stock(item["formula_id"]) - item["quantity"] + qty < -EPS:
        flash("Impossible : une partie de cette provende a déjà été distribuée ou sortie.", "error")
        return back
    formula = _formula(item["formula_id"])
    old_lines = query("SELECT material_id FROM production_lines WHERE production_id = ?", (production_id,))
    errors = []

    class _Cancel(Exception):
        pass

    try:
        with transaction() as conn:
            conn.execute("DELETE FROM stock_moves WHERE ref_type = 'production' AND ref_id = ?", (production_id,))
            conn.execute("DELETE FROM feed_moves WHERE ref_type = 'production' AND ref_id = ?", (production_id,))
            conn.execute("DELETE FROM production_lines WHERE production_id = ?", (production_id,))
            for row in old_lines:
                recompute_material(conn, row["material_id"])
            needs = production_needs(formula, qty, conn)
            for n in needs:
                if n["missing"] > EPS:
                    errors.append(f"Stock insuffisant de « {n['name']} » : il faut {fmt_qty(n['need'], n['unit'])}, "
                                  f"il y a {fmt_qty(n['stock'], n['unit'])}.")
            if errors:
                raise _Cancel()
            cost_total = sum(n["cost"] for n in needs)
            conn.execute("UPDATE productions SET date = ?, quantity = ?, cost_total = ?, cost_per_kg = ?, notes = ? WHERE id = ?",
                         (date, qty, cost_total, cost_total / qty, notes, production_id))
            for n in needs:
                conn.execute("INSERT INTO production_lines (production_id, material_id, quantity, unit_cost) VALUES (?, ?, ?, ?)",
                             (production_id, n["material_id"], n["need"], n["unit_cost"]))
                conn.execute(
                    """INSERT INTO stock_moves (date, material_id, quantity, unit_cost, kind, ref_type, ref_id, created_by, created_at)
                       VALUES (?, ?, ?, ?, 'fabrication', 'production', ?, ?, ?)""",
                    (date, n["material_id"], -n["need"], n["unit_cost"], production_id, item["created_by"], item["created_at"]),
                )
                recompute_material(conn, n["material_id"])
            conn.execute(
                """INSERT INTO feed_moves (date, formula_id, quantity, unit_cost, kind, ref_type, ref_id, created_by, created_at)
                   VALUES (?, ?, ?, ?, 'fabrication', 'production', ?, ?, ?)""",
                (date, formula["id"], qty, cost_total / qty, production_id, item["created_by"], item["created_at"]),
            )
            recompute_formula(conn, formula["id"])
    except _Cancel:
        pass
    if errors:
        for message in errors:
            flash(message, "error")
        return back
    changes = []
    if abs(qty - item["quantity"]) > EPS:
        changes.append(f"quantité {fmt_qty(item['quantity'], 'kg')} → {fmt_qty(qty, 'kg')}")
    if date != item["date"]:
        changes.append(f"date {date_fr(item['date'])} → {date_fr(date)}")
    log_activity("Fabrication corrigée", f"n°{production_id} ({formula['name']}) : " + ("; ".join(changes) or "remarque modifiée"))
    flash("Fabrication corrigée : les matières et la provende ont été recalculées.", "success")
    return back


@bp.route("/fabrications/<int:production_id>/supprimer", methods=["POST"])
@require("provenderie", MANAGE)
def production_delete(production_id):
    item = query("SELECT * FROM productions WHERE id = ?", (production_id,), one=True)
    if item is None:
        abort(404)
    if feed_stock(item["formula_id"]) - item["quantity"] < -EPS:
        flash("Impossible d'annuler : une partie de cette provende a déjà été distribuée ou sortie.", "error")
        return redirect(url_for("provenderie.production", production_id=production_id))
    materials = [r["material_id"] for r in query("SELECT material_id FROM production_lines WHERE production_id = ?",
                                                  (production_id,))]
    with transaction() as conn:
        conn.execute("DELETE FROM stock_moves WHERE ref_type = 'production' AND ref_id = ?", (production_id,))
        conn.execute("DELETE FROM feed_moves WHERE ref_type = 'production' AND ref_id = ?", (production_id,))
        conn.execute("DELETE FROM production_lines WHERE production_id = ?", (production_id,))
        conn.execute("DELETE FROM productions WHERE id = ?", (production_id,))
        for material_id in materials:
            recompute_material(conn, material_id)
        recompute_formula(conn, item["formula_id"])
    log_activity("Fabrication annulée", f"n°{production_id} — {fmt_qty(item['quantity'], 'kg')}")
    notify("", None, f"↩️ Fabrication n°{production_id} annulée ({fmt_qty(item['quantity'], 'kg')}) : "
           "les matières sont revenues en stock.")
    flash("Fabrication annulée : les matières sont revenues en stock.", "success")
    return redirect(url_for("provenderie.productions"))


# ---------------------------------------------------------------------------
# Mouvements de provende : perte, inventaire, entrée
# ---------------------------------------------------------------------------
@bp.route("/mouvement", methods=["GET", "POST"])
@require("provenderie", EDIT)
def movement():
    formulas = _active_formulas()
    form = {"formula_id": request.args.get("formule", ""), "kind": request.args.get("type", "perte"),
            "quantity": "", "unit_cost": "", "date": today(), "notes": ""}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        qty = parse_num(form["quantity"])
        cost = parse_num(form["unit_cost"], 0) or 0
        formula = query("SELECT * FROM formulas WHERE id = ?", (form["formula_id"] or 0,), one=True)
        errors = []
        if formula is None:
            errors.append("Choisissez une provende.")
        if form["kind"] not in ("perte", "inventaire", "entree"):
            errors.append("Type invalide.")
        if qty is None or qty < 0 or (qty == 0 and form["kind"] != "inventaire"):
            errors.append("Indiquez une quantité valide (kg).")
        if cost < 0:
            errors.append("Le prix ne peut pas être négatif.")
        if not valid_date(form["date"]):
            errors.append("Date invalide.")
        stock = feed_stock(formula["id"]) if formula else 0
        if formula and qty is not None and form["kind"] == "perte" and qty > stock + EPS:
            errors.append(f"Stock insuffisant : il reste {fmt_qty(stock, 'kg')}.")
        signed = 0
        if formula and qty is not None:
            signed = -qty if form["kind"] == "perte" else (qty - stock if form["kind"] == "inventaire" else qty)
            if form["kind"] == "inventaire" and abs(signed) < EPS:
                errors.append("Aucun écart : le stock compté est égal au stock du logiciel.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            unit_cost = cost if form["kind"] == "entree" else formula["avg_cost"]
            with transaction() as conn:
                conn.execute(
                    """INSERT INTO feed_moves (date, formula_id, quantity, unit_cost, kind, notes, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (form["date"], formula["id"], signed, unit_cost, form["kind"], form["notes"], g.user["id"], now_utc()),
                )
                recompute_formula(conn, formula["id"])
            log_activity(FEED_KINDS[form["kind"]], f"{formula['name']} : {fmt_qty(signed, 'kg')}")
            if form["kind"] in ("perte", "inventaire"):
                label = "⚠️ Perte de provende" if form["kind"] == "perte" else "📋 Écart d'inventaire de provende"
                notify("formula", formula["id"], f"{label} : {formula['name']} {'+' if signed > 0 else ''}"
                       f"{fmt_qty(signed, 'kg')}" + (f" — motif : {form['notes']}" if form["notes"] else ""))
            flash(f"{FEED_KINDS[form['kind']]} enregistrée : {formula['name']} {'+' if signed > 0 else ''}{fmt_qty(signed, 'kg')}.",
                  "success")
            return redirect(url_for("provenderie.feed_history", formula_id=formula["id"]))
    return render_template("provenderie/movement.html", form=form, formulas=formulas)


@bp.route("/stock/<int:formula_id>")
@require("provenderie", VIEW)
def feed_history(formula_id):
    item = _formula(formula_id)
    moves = query("""SELECT s.*, u.username FROM feed_moves s LEFT JOIN users u ON u.id = s.created_by
                     WHERE s.formula_id = ? ORDER BY s.date DESC, s.id DESC LIMIT 200""", (formula_id,))
    return render_template("provenderie/feed_history.html", item=item, moves=moves, stock=feed_stock(formula_id),
                           kinds=FEED_KINDS)


@bp.route("/historique")
@require("provenderie", VIEW)
def feed_history_all():
    """Stock de provende et tout son historique (fabrications, distributions, pertes…)."""
    f = {"formule": request.args.get("formule", type=int), "type": request.args.get("type", ""),
         "du": request.args.get("du", ""), "au": request.args.get("au", "")}
    moves = moves_with_balance("feed", item_id=f["formule"], move_kind=f["type"] or None,
                               since=f["du"] if valid_date(f["du"]) else None,
                               until=f["au"] if valid_date(f["au"]) else None)
    formulas = query("SELECT id, name FROM formulas ORDER BY name")
    return render_template("provenderie/history.html", moves=moves, filters=f, formulas=formulas, kinds=FEED_KINDS,
                           overview=formulas_overview(), grams=_program_grams())


def _program_grams():
    """g / poule / jour prévus dans les programmes, par formule (pour l'estimation de durée)."""
    rows = query("""SELECT w.formula_id, AVG(w.grams_per_bird) AS g FROM feed_program_weeks w
                    JOIN feed_programs p ON p.id = w.program_id
                    WHERE p.active = 1 AND w.grams_per_bird > 0 GROUP BY w.formula_id""")
    return {r["formula_id"]: r["g"] for r in rows}


@bp.route("/stock/mouvement/<int:move_id>/corriger", methods=["POST"])
@require("provenderie", EDIT)
def feed_move_edit(move_id):
    move = query("SELECT * FROM feed_moves WHERE id = ?", (move_id,), one=True)
    if move is None:
        abort(404)
    if not can_correct("provenderie", move):
        abort(403)
    back = redirect(url_for("provenderie.feed_history", formula_id=move["formula_id"]))
    if move["kind"] not in ("perte", "inventaire", "entree"):
        flash("Ce mouvement vient d'une fabrication ou d'une distribution : corrigez-la à sa source.", "error")
        return back
    qty = parse_num(request.form.get("quantity"))
    cost = parse_num(request.form.get("unit_cost"), move["unit_cost"])
    date = request.form.get("date", move["date"])
    notes = request.form.get("notes", move["notes"] or "").strip()
    if qty is None or (move["kind"] != "inventaire" and qty <= 0) or (move["kind"] == "inventaire" and abs(qty) < EPS):
        flash("Quantité invalide.", "error")
        return back
    if cost is None or cost < 0 or not valid_date(date):
        flash("Prix ou date invalide.", "error")
        return back
    signed = -qty if move["kind"] == "perte" else qty
    unit_cost = cost if move["kind"] == "entree" else move["unit_cost"]
    after = feed_stock(move["formula_id"]) - move["quantity"] + signed
    if after < -EPS:
        flash(f"Impossible : le stock deviendrait {fmt_qty(after, 'kg')}.", "error")
        return back
    with transaction() as conn:
        conn.execute("UPDATE feed_moves SET quantity = ?, unit_cost = ?, date = ?, notes = ? WHERE id = ?",
                     (signed, unit_cost, date, notes, move_id))
        recompute_formula(conn, move["formula_id"])
    formula = _formula(move["formula_id"])
    log_activity("Mouvement de provende corrigé", f"{formula['name']} : {fmt_qty(move['quantity'], 'kg')} → {fmt_qty(signed, 'kg')}")
    notify("formula", formula["id"], f"✏️ Correction provende {formula['name']} : "
           f"{fmt_qty(move['quantity'], 'kg')} → {fmt_qty(signed, 'kg')}")
    flash("Correction enregistrée.", "success")
    return back


@bp.route("/stock/mouvement/<int:move_id>/supprimer", methods=["POST"])
@require("provenderie", MANAGE)
def feed_move_delete(move_id):
    move = query("SELECT * FROM feed_moves WHERE id = ?", (move_id,), one=True)
    if move is None:
        abort(404)
    if move["kind"] not in ("perte", "inventaire", "entree"):
        flash("Ce mouvement vient d'une fabrication ou d'une distribution : annulez-la à sa source.", "error")
    elif move["quantity"] > 0 and feed_stock(move["formula_id"]) - move["quantity"] < -EPS:
        flash("Impossible : cette provende a déjà été utilisée.", "error")
    else:
        with transaction() as conn:
            conn.execute("DELETE FROM feed_moves WHERE id = ?", (move_id,))
            recompute_formula(conn, move["formula_id"])
        log_activity("Mouvement de provende annulé", f"n°{move_id}")
        _f = _formula(move["formula_id"])
        notify("formula", move["formula_id"], f"↩️ Mouvement de provende annulé : {_f['name']} {fmt_qty(move['quantity'], 'kg')}")
        flash("Mouvement annulé.", "success")
    return redirect(url_for("provenderie.feed_history", formula_id=move["formula_id"]))
