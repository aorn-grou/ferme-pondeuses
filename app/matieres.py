"""Module Achats & matières premières : matières, catégories, fournisseurs, achats, stock."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .security import EDIT, MANAGE, VIEW, can, can_correct, require
from .stock import (
    EPS, accounts, inventory_gaps, last_moves, moves_with_balance, material_stock, materials_overview, parse_num, recompute_material, supplier_balance,
    today, valid_date,
)
from .utils import date_fr, fmt_money, fmt_qty, log_activity

from .discussion import notify  # noqa: E402

bp = Blueprint("matieres", __name__, url_prefix="/matieres")

UNITS = ["kg", "g", "tonne", "sac", "L", "mL", "unité", "pièce", "dose", "flacon", "boîte", "sachet", "carton", "plateau"]
NEW = "__new__"  # option « Autre : écrire… » des listes déroulantes
MOVE_KINDS = {
    "achat": "Achat",
    "fabrication": "Fabrication de provende",
    "perte": "Perte / casse",
    "inventaire": "Correction d'inventaire",
    "stock_initial": "Stock initial / entrée",
}


def _material(material_id):
    row = query("SELECT m.*, c.name AS category FROM materials m LEFT JOIN categories c ON c.id = m.category_id "
                "WHERE m.id = ?", (material_id,), one=True)
    if row is None:
        abort(404)
    return row


def _categories():
    return query("SELECT * FROM categories WHERE kind = 'matiere' AND active = 1 ORDER BY name")


def _suppliers(active_only=True):
    return query(f"SELECT * FROM suppliers {'WHERE active = 1' if active_only else ''} ORDER BY name")


def _back(default_endpoint, **kw):
    target = request.form.get("next") or request.args.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(url_for(default_endpoint, **kw))


# ---------------------------------------------------------------------------
# Vue d'ensemble du stock
# ---------------------------------------------------------------------------
@bp.route("/")
@require("matieres", VIEW)
def index():
    materials = materials_overview()
    category = request.args.get("categorie", "")
    shown = [m for m in materials if not category or str(m["category_id"]) == category]
    due_total = sum(max(0, supplier_balance(s["id"])["due"]) for s in _suppliers(active_only=False))
    month = today()[:7]
    month_purchases = query("SELECT COALESCE(SUM(total), 0) AS t FROM purchases WHERE substr(date, 1, 7) = ?",
                            (month,), one=True)["t"]
    summary = {
        "value": sum(m["value"] for m in materials),
        "alerts": sum(1 for m in materials if m["status"] in ("low", "empty")),
        "due": due_total,
        "month_purchases": month_purchases,
        "count": len(materials),
    }
    archived = query("SELECT COUNT(*) AS n FROM materials WHERE active = 0", one=True)["n"]
    gaps = inventory_gaps(since=_days_ago(30))
    last_gap = {}
    for gap in inventory_gaps():
        last_gap.setdefault(gap["material_id"], gap)
    summary["gap_value"] = sum(g_["value"] for g_ in gaps)
    return render_template("matieres/index.html", materials=shown, summary=summary, categories=_categories(),
                           category=category, archived=archived, gaps=gaps, last_gap=last_gap,
                           last_move=last_moves(), kinds=MOVE_KINDS)


def _days_ago(days):
    from datetime import timedelta

    from .utils import local_now
    return (local_now() - timedelta(days=days)).strftime("%Y-%m-%d")


@bp.route("/historique")
@require("matieres", VIEW)
def history():
    """Tout l'historique du stock des matières : achats, fabrications, pertes, inventaires…"""
    f = {"matiere": request.args.get("matiere", type=int), "type": request.args.get("type", ""),
         "du": request.args.get("du", ""), "au": request.args.get("au", "")}
    moves = moves_with_balance("material", item_id=f["matiere"], move_kind=f["type"] or None,
                               since=f["du"] if valid_date(f["du"]) else None,
                               until=f["au"] if valid_date(f["au"]) else None)
    entries = sum(m["quantity"] for m in moves if m["quantity"] > 0)
    exits = sum(-m["quantity"] for m in moves if m["quantity"] < 0)
    materials = query("SELECT id, name FROM materials ORDER BY name")
    return render_template("matieres/history.html", moves=moves, filters=f, materials=materials, kinds=MOVE_KINDS,
                           overview=materials_overview(), entries=entries, exits=exits)


@bp.route("/archives")
@require("matieres", VIEW)
def archived():
    rows = [m for m in materials_overview(include_inactive=True) if not m["active"]]
    return render_template("matieres/archived.html", materials=rows)


# ---------------------------------------------------------------------------
# Fiche matière
# ---------------------------------------------------------------------------
def get_or_create_category(name):
    """Retrouve une catégorie par son nom (sans tenir compte des majuscules) ou la crée."""
    row = query("SELECT id FROM categories WHERE kind = 'matiere' AND name = ? COLLATE NOCASE", (name,), one=True)
    if row:
        execute("UPDATE categories SET active = 1 WHERE id = ?", (row["id"],))
        return row["id"]
    log_activity("Catégorie ajoutée", name)
    return execute("INSERT INTO categories (kind, name) VALUES ('matiere', ?)", (name,))


def _material_form_values():
    category_id = request.form.get("category_id") or None
    category_new = ""
    if "category_name" in request.form:  # catégorie écrite librement (ou choisie dans les suggestions)
        category_new = " ".join(request.form.get("category_name", "").split())[:60]
        category_id = NEW if category_new else None
    elif category_id == NEW:
        category_new = request.form.get("category_new", "").strip()
    unit = " ".join(request.form.get("unit", "kg").split())
    if unit == NEW:
        unit = request.form.get("unit_new", "").strip()
    return {
        "name": request.form.get("name", "").strip(),
        "category_id": category_id,
        "category_new": category_new,
        "unit": unit[:15] or "kg",
        "alert_threshold": parse_num(request.form.get("alert_threshold"), 0) or 0,
        "notes": request.form.get("notes", "").strip(),
    }


def _material_errors(values, material_id=None):
    errors = []
    if len(values["name"]) < 2:
        errors.append("Donnez un nom à la matière (au moins 2 caractères).")
    dup = query("SELECT id FROM materials WHERE name = ? COLLATE NOCASE AND id != ?",
                (values["name"], material_id or 0), one=True)
    if dup:
        errors.append("Une matière porte déjà ce nom.")
    if values["alert_threshold"] < 0:
        errors.append("Le seuil d'alerte ne peut pas être négatif.")
    if values["category_id"] == NEW and len(values["category_new"]) < 2:
        errors.append("Le nom de la catégorie est trop court (au moins 2 lettres).")
    return errors


def _apply_new_category(values):
    if values["category_id"] == NEW:
        values["category_id"] = get_or_create_category(values["category_new"][:60])


@bp.route("/matiere/nouvelle", methods=["GET", "POST"])
@require("matieres", EDIT)
def material_new():
    values = {"name": "", "category_id": None, "unit": "kg", "alert_threshold": 0, "notes": ""}
    if request.method == "POST":
        values = _material_form_values()
        initial_qty = parse_num(request.form.get("initial_qty"), 0) or 0
        initial_cost = parse_num(request.form.get("initial_cost"), 0) or 0
        errors = _material_errors(values)
        if initial_qty < 0 or initial_cost < 0:
            errors.append("Le stock initial et son prix ne peuvent pas être négatifs.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            _apply_new_category(values)
            with transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO materials (name, category_id, unit, alert_threshold, notes, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (values["name"], values["category_id"], values["unit"], values["alert_threshold"],
                     values["notes"], now_utc()),
                )
                material_id = cur.lastrowid
                if initial_qty > 0:
                    conn.execute(
                        """INSERT INTO stock_moves (date, material_id, quantity, unit_cost, kind, notes, created_by, created_at)
                           VALUES (?, ?, ?, ?, 'stock_initial', 'Stock initial', ?, ?)""",
                        (today(), material_id, initial_qty, initial_cost, g.user["id"], now_utc()),
                    )
                    recompute_material(conn, material_id)
            log_activity("Matière créée", values["name"])
            flash(f"Matière « {values['name']} » ajoutée.", "success")
            return _back("matieres.index")
    return render_template("matieres/material_form.html", values=values, material=None,
                           categories=_categories(), units=UNITS)


@bp.route("/matiere/<int:material_id>")
@require("matieres", VIEW)
def material(material_id):
    item = _material(material_id)
    stock = material_stock(material_id)
    moves = query(
        """SELECT s.*, u.username FROM stock_moves s LEFT JOIN users u ON u.id = s.created_by
           WHERE s.material_id = ? ORDER BY s.date DESC, s.id DESC LIMIT 200""",
        (material_id,),
    )
    used_in = query(
        """SELECT f.id, f.name, l.quantity, f.base_qty FROM formula_lines l JOIN formulas f ON f.id = l.formula_id
           WHERE l.material_id = ? AND f.active = 1 ORDER BY f.name""",
        (material_id,),
    )
    return render_template("matieres/material.html", item=item, stock=stock, moves=moves, kinds=MOVE_KINDS,
                           used_in=used_in)


@bp.route("/matiere/<int:material_id>/modifier", methods=["GET", "POST"])
@require("matieres", EDIT)
def material_edit(material_id):
    item = _material(material_id)
    values = dict(item)
    if request.method == "POST":
        values = _material_form_values()
        errors = _material_errors(values, material_id)
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            _apply_new_category(values)
            execute(
                "UPDATE materials SET name = ?, category_id = ?, unit = ?, alert_threshold = ?, notes = ? WHERE id = ?",
                (values["name"], values["category_id"], values["unit"], values["alert_threshold"], values["notes"],
                 material_id),
            )
            log_activity("Matière modifiée", values["name"])
            flash("Modifications enregistrées.", "success")
            return redirect(url_for("matieres.material", material_id=material_id))
    return render_template("matieres/material_form.html", values=values, material=item,
                           categories=_categories(), units=UNITS)


@bp.route("/matiere/<int:material_id>/archiver", methods=["POST"])
@require("matieres", MANAGE)
def material_archive(material_id):
    item = _material(material_id)
    has_moves = query("SELECT 1 FROM stock_moves WHERE material_id = ? LIMIT 1", (material_id,), one=True)
    in_formula = query("SELECT 1 FROM formula_lines l JOIN formulas f ON f.id = l.formula_id "
                       "WHERE l.material_id = ? AND f.active = 1 LIMIT 1", (material_id,), one=True)
    action = request.form.get("action")
    if action == "delete":
        if has_moves or in_formula:
            flash("Cette matière a un historique ou sert dans une formule : elle peut être archivée, pas supprimée.",
                  "error")
            return redirect(url_for("matieres.material", material_id=material_id))
        execute("DELETE FROM materials WHERE id = ?", (material_id,))
        log_activity("Matière supprimée", item["name"])
        flash(f"Matière « {item['name']} » supprimée.", "success")
        return redirect(url_for("matieres.index"))
    new_state = 0 if item["active"] else 1
    if not new_state and in_formula:
        flash("Retirez d'abord cette matière des formules de provende actives.", "error")
        return redirect(url_for("matieres.material", material_id=material_id))
    execute("UPDATE materials SET active = ? WHERE id = ?", (new_state, material_id))
    log_activity("Matière " + ("réactivée" if new_state else "archivée"), item["name"])
    flash(f"Matière « {item['name']} » " + ("réactivée." if new_state else "archivée : elle n'apparaît plus dans les listes."),
          "success")
    return redirect(url_for("matieres.material", material_id=material_id))


# ---------------------------------------------------------------------------
# Catégories
# ---------------------------------------------------------------------------
@bp.route("/categories", methods=["GET", "POST"])
@require("matieres", EDIT)
def categories():
    if request.method == "POST":
        action = request.form.get("action")
        name = request.form.get("name", "").strip()
        cat_id = request.form.get("id", type=int)
        if action == "add":
            if len(name) < 2:
                flash("Donnez un nom à la catégorie.", "error")
            elif query("SELECT 1 FROM categories WHERE kind='matiere' AND active=1 AND name = ? COLLATE NOCASE",
                       (name,), one=True):
                flash("Cette catégorie existe déjà.", "error")
            else:
                execute("INSERT INTO categories (kind, name) VALUES ('matiere', ?)", (name,))
                log_activity("Catégorie ajoutée", name)
                flash(f"Catégorie « {name} » ajoutée.", "success")
        elif action == "rename" and cat_id:
            if len(name) < 2:
                flash("Donnez un nom à la catégorie.", "error")
            else:
                execute("UPDATE categories SET name = ? WHERE id = ? AND kind = 'matiere'", (name, cat_id))
                log_activity("Catégorie renommée", name)
                flash("Catégorie renommée.", "success")
        elif action == "delete" and cat_id:
            used = query("SELECT COUNT(*) AS n FROM materials WHERE category_id = ?", (cat_id,), one=True)["n"]
            if used:
                flash(f"Impossible : {used} matière(s) utilisent cette catégorie. Changez-les d'abord.", "error")
            else:
                execute("DELETE FROM categories WHERE id = ? AND kind = 'matiere'", (cat_id,))
                log_activity("Catégorie supprimée", str(cat_id))
                flash("Catégorie supprimée.", "success")
        return redirect(url_for("matieres.categories"))
    rows = query("""SELECT c.*, (SELECT COUNT(*) FROM materials m WHERE m.category_id = c.id) AS used
                    FROM categories c WHERE c.kind = 'matiere' AND c.active = 1 ORDER BY c.name""")
    return render_template("matieres/categories.html", rows=rows)


# ---------------------------------------------------------------------------
# Mouvements manuels : stock initial, entrée, perte
# ---------------------------------------------------------------------------
@bp.route("/mouvement", methods=["GET", "POST"])
@require("matieres", EDIT)
def movement():
    materials = query("SELECT * FROM materials WHERE active = 1 ORDER BY name")
    form = {"material_id": request.args.get("matiere", ""), "kind": request.args.get("type", "perte"),
            "quantity": "", "unit_cost": "", "date": today(), "notes": ""}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        qty = parse_num(form["quantity"])
        cost = parse_num(form["unit_cost"], 0) or 0
        item = query("SELECT * FROM materials WHERE id = ?", (form["material_id"] or 0,), one=True)
        errors = []
        if item is None:
            errors.append("Choisissez une matière.")
        if form["kind"] not in ("perte", "stock_initial"):
            errors.append("Type de mouvement invalide.")
        if qty is None or qty <= 0:
            errors.append("Indiquez une quantité supérieure à 0.")
        if cost < 0:
            errors.append("Le prix ne peut pas être négatif.")
        if not valid_date(form["date"]):
            errors.append("Date invalide.")
        if item is not None and qty and form["kind"] == "perte" and qty > material_stock(item["id"]) + EPS:
            errors.append(f"Stock insuffisant : il reste {fmt_qty(material_stock(item['id']), item['unit'])}.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            signed = -qty if form["kind"] == "perte" else qty
            unit_cost = item["avg_cost"] if form["kind"] == "perte" else cost
            with transaction() as conn:
                conn.execute(
                    """INSERT INTO stock_moves (date, material_id, quantity, unit_cost, kind, notes, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (form["date"], item["id"], signed, unit_cost, form["kind"], form["notes"], g.user["id"], now_utc()),
                )
                recompute_material(conn, item["id"])
            log_activity(MOVE_KINDS[form["kind"]], f"{item['name']} : {fmt_qty(signed, item['unit'])}")
            if form["kind"] == "perte":
                notify("material", item["id"], f"⚠️ Perte déclarée : {item['name']} {fmt_qty(signed, item['unit'])} "
                       f"(≈ {fmt_money(abs(signed) * unit_cost)})" + (f" — motif : {form['notes']}" if form["notes"] else ""))
            flash(f"{MOVE_KINDS[form['kind']]} enregistrée : {item['name']} {fmt_qty(signed, item['unit'])}.", "success")
            return redirect(url_for("matieres.material", material_id=item["id"]))
    return render_template("matieres/movement.html", form=form, materials=materials)


@bp.route("/mouvement/<int:move_id>/supprimer", methods=["POST"])
@require("matieres", MANAGE)
def movement_delete(move_id):
    move = query("SELECT * FROM stock_moves WHERE id = ?", (move_id,), one=True)
    if move is None:
        abort(404)
    if move["kind"] not in ("perte", "stock_initial", "inventaire"):
        flash("Ce mouvement vient d'un achat ou d'une fabrication : supprimez l'achat ou la fabrication.", "error")
        return redirect(url_for("matieres.material", material_id=move["material_id"]))
    if move["quantity"] > 0 and material_stock(move["material_id"]) - move["quantity"] < -EPS:
        flash("Impossible : cette quantité a déjà été utilisée (le stock deviendrait négatif).", "error")
        return redirect(url_for("matieres.material", material_id=move["material_id"]))
    with transaction() as conn:
        conn.execute("DELETE FROM stock_moves WHERE id = ?", (move_id,))
        recompute_material(conn, move["material_id"])
    log_activity("Mouvement de stock annulé", f"n°{move_id}")
    _mat = query("SELECT name, unit FROM materials WHERE id = ?", (move["material_id"],), one=True)
    if _mat:
        notify("material", move["material_id"], f"↩️ Mouvement annulé : {_mat['name']} "
               f"{'+' if move['quantity'] > 0 else ''}{fmt_qty(move['quantity'], _mat['unit'])} du {date_fr(move['date'])}")
    flash("Mouvement annulé.", "success")
    return redirect(url_for("matieres.material", material_id=move["material_id"]))


@bp.route("/mouvement/<int:move_id>/corriger", methods=["POST"])
@require("matieres", EDIT)
def movement_edit(move_id):
    move = query("SELECT * FROM stock_moves WHERE id = ?", (move_id,), one=True)
    if move is None:
        abort(404)
    if not can_correct("matieres", move):
        abort(403)
    item = _material(move["material_id"])
    back = redirect(url_for("matieres.material", material_id=item["id"]))
    if move["kind"] not in ("perte", "stock_initial", "inventaire"):
        flash("Ce mouvement vient d'un achat ou d'une fabrication : corrigez l'achat ou la fabrication.", "error")
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
    unit_cost = cost if move["kind"] == "stock_initial" else move["unit_cost"]
    after = material_stock(item["id"]) - move["quantity"] + signed
    if after < -EPS:
        flash(f"Impossible : le stock deviendrait {fmt_qty(after, item['unit'])}.", "error")
        return back
    changes = []
    if abs(signed - move["quantity"]) > EPS:
        changes.append(f"quantité {fmt_qty(move['quantity'], item['unit'])} → {fmt_qty(signed, item['unit'])}")
    if abs(unit_cost - move["unit_cost"]) > EPS:
        changes.append(f"prix {fmt_money(move['unit_cost'])} → {fmt_money(unit_cost)}")
    if date != move["date"]:
        changes.append(f"date {date_fr(move['date'])} → {date_fr(date)}")
    with transaction() as conn:
        conn.execute("UPDATE stock_moves SET quantity = ?, unit_cost = ?, date = ?, notes = ? WHERE id = ?",
                     (signed, unit_cost, date, notes, move_id))
        recompute_material(conn, item["id"])
    log_activity("Mouvement de stock corrigé", f"{item['name']} ({MOVE_KINDS.get(move['kind'], move['kind'])}) : "
                 + ("; ".join(changes) or "remarque modifiée"))
    if changes:
        notify("material", item["id"], f"✏️ Correction sur {item['name']} ({MOVE_KINDS.get(move['kind'], move['kind'])}) : "
               + "; ".join(changes))
    flash("Correction enregistrée : " + ("; ".join(changes) or "remarque modifiée") + ".", "success")
    return back


# ---------------------------------------------------------------------------
# Inventaire physique
# ---------------------------------------------------------------------------
@bp.route("/inventaire", methods=["GET", "POST"])
@require("matieres", EDIT)
def inventory():
    materials = materials_overview()
    if request.method == "POST":
        date = request.form.get("date", today())
        if not valid_date(date):
            flash("Date invalide.", "error")
            return redirect(url_for("matieres.inventory"))
        changes = []
        for m in materials:
            counted = parse_num(request.form.get(f"count_{m['id']}"))
            if counted is None:
                continue
            if counted < 0:
                flash(f"{m['name']} : la quantité comptée ne peut pas être négative.", "error")
                return redirect(url_for("matieres.inventory"))
            diff = counted - m["stock"]
            if abs(diff) > EPS:
                changes.append((m, diff))
        if not changes:
            flash("Aucun écart : le stock compté correspond au stock du logiciel.", "info")
            return redirect(url_for("matieres.index"))
        with transaction() as conn:
            for m, diff in changes:
                why = " ".join(request.form.get(f"why_{m['id']}", "").split())[:150]
                conn.execute(
                    """INSERT INTO stock_moves (date, material_id, quantity, unit_cost, kind, notes, created_by, created_at)
                       VALUES (?, ?, ?, ?, 'inventaire', ?, ?, ?)""",
                    (date, m["id"], diff, m["avg_cost"], why, g.user["id"], now_utc()),
                )
                recompute_material(conn, m["id"])
        details = ", ".join(f"{m['name']} {'+' if d > 0 else ''}{fmt_qty(d, m['unit'])}" for m, d in changes)
        log_activity("Inventaire des matières", details)
        for m, d in changes:
            why = " ".join(request.form.get(f"why_{m['id']}", "").split())[:150]
            notify("material", m["id"], f"📋 Écart d'inventaire : {m['name']} — logiciel {fmt_qty(m['stock'], m['unit'])}, "
                   f"compté {fmt_qty(m['stock'] + d, m['unit'])} → écart {'+' if d > 0 else ''}{fmt_qty(d, m['unit'])} "
                   f"(≈ {'+' if d > 0 else '-'}{fmt_money(abs(d) * m['avg_cost'])})"
                   + (f" — explication : {why}" if why else " — ⚠️ sans explication"))
        flash(f"Inventaire enregistré : {len(changes)} correction(s). {details}", "success")
        return redirect(url_for("matieres.index"))
    return render_template("matieres/inventory.html", materials=materials, today=today())


# ---------------------------------------------------------------------------
# Fournisseurs
# ---------------------------------------------------------------------------
@bp.route("/fournisseurs")
@require("matieres", VIEW)
def suppliers():
    rows = []
    for s in _suppliers(active_only=False):
        item = dict(s)
        item.update(supplier_balance(s["id"]))
        rows.append(item)
    return render_template("matieres/suppliers.html", rows=rows)


def _supplier_values():
    return {k: request.form.get(k, "").strip() for k in ("name", "phone", "address", "notes")}


@bp.route("/fournisseurs/nouveau", methods=["GET", "POST"])
@require("matieres", EDIT)
def supplier_new():
    values = {"name": "", "phone": "", "address": "", "notes": ""}
    if request.method == "POST":
        values = _supplier_values()
        if len(values["name"]) < 2:
            flash("Donnez le nom du fournisseur.", "error")
        elif query("SELECT 1 FROM suppliers WHERE name = ? COLLATE NOCASE", (values["name"],), one=True):
            flash("Un fournisseur porte déjà ce nom.", "error")
        else:
            supplier_id = execute(
                "INSERT INTO suppliers (name, phone, address, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                (values["name"], values["phone"], values["address"], values["notes"], now_utc()),
            )
            log_activity("Fournisseur ajouté", values["name"])
            flash(f"Fournisseur « {values['name']} » ajouté.", "success")
            if request.form.get("next"):
                return _back("matieres.suppliers")
            return redirect(url_for("matieres.supplier", supplier_id=supplier_id))
    return render_template("matieres/supplier_form.html", values=values, supplier=None)


def _supplier(supplier_id):
    row = query("SELECT * FROM suppliers WHERE id = ?", (supplier_id,), one=True)
    if row is None:
        abort(404)
    return row


@bp.route("/fournisseurs/<int:supplier_id>")
@require("matieres", VIEW)
def supplier(supplier_id):
    item = _supplier(supplier_id)
    purchases = query("SELECT * FROM purchases WHERE supplier_id = ? ORDER BY date DESC, id DESC", (supplier_id,))
    payments = query("""SELECT p.*, a.name AS account FROM supplier_payments p LEFT JOIN accounts a ON a.id = p.account_id
                        WHERE p.supplier_id = ? ORDER BY p.date DESC, p.id DESC""", (supplier_id,))
    return render_template("matieres/supplier.html", item=item, purchases=purchases, payments=payments,
                           balance=supplier_balance(supplier_id), accounts=accounts(), today=today())


@bp.route("/fournisseurs/<int:supplier_id>/modifier", methods=["GET", "POST"])
@require("matieres", EDIT)
def supplier_edit(supplier_id):
    item = _supplier(supplier_id)
    values = dict(item)
    if request.method == "POST":
        values = _supplier_values()
        if len(values["name"]) < 2:
            flash("Donnez le nom du fournisseur.", "error")
        elif query("SELECT 1 FROM suppliers WHERE name = ? COLLATE NOCASE AND id != ?", (values["name"], supplier_id),
                   one=True):
            flash("Un fournisseur porte déjà ce nom.", "error")
        else:
            execute("UPDATE suppliers SET name = ?, phone = ?, address = ?, notes = ? WHERE id = ?",
                    (values["name"], values["phone"], values["address"], values["notes"], supplier_id))
            log_activity("Fournisseur modifié", values["name"])
            flash("Modifications enregistrées.", "success")
            return redirect(url_for("matieres.supplier", supplier_id=supplier_id))
    return render_template("matieres/supplier_form.html", values=values, supplier=item)


@bp.route("/fournisseurs/<int:supplier_id>/archiver", methods=["POST"])
@require("matieres", MANAGE)
def supplier_archive(supplier_id):
    item = _supplier(supplier_id)
    execute("UPDATE suppliers SET active = ? WHERE id = ?", (0 if item["active"] else 1, supplier_id))
    log_activity("Fournisseur " + ("archivé" if item["active"] else "réactivé"), item["name"])
    flash("Fournisseur " + ("archivé." if item["active"] else "réactivé."), "success")
    return redirect(url_for("matieres.supplier", supplier_id=supplier_id))


@bp.route("/fournisseurs/<int:supplier_id>/reglement", methods=["POST"])
@require("matieres", EDIT)
def supplier_payment(supplier_id):
    item = _supplier(supplier_id)
    amount = parse_num(request.form.get("amount"))
    date = request.form.get("date", today())
    account_id = request.form.get("account_id", type=int)
    due = supplier_balance(supplier_id)["due"]
    account = query("SELECT * FROM accounts WHERE id = ?", (account_id or 0,), one=True)
    if amount is None or amount <= 0:
        flash("Indiquez un montant supérieur à 0.", "error")
    elif amount > due + 0.5:
        flash(f"Le montant dépasse la dette ({fmt_money(due)}).", "error")
    elif account is None:
        flash("Choisissez d'où vient l'argent.", "error")
    elif not valid_date(date):
        flash("Date invalide.", "error")
    else:
        with transaction() as conn:
            cur = conn.execute(
                """INSERT INTO supplier_payments (supplier_id, date, amount, account_id, notes, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (supplier_id, date, amount, account_id, request.form.get("notes", "").strip(), g.user["id"], now_utc()),
            )
            conn.execute(
                """INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                   VALUES (?, ?, ?, 'reglement_fournisseur', ?, 'supplier_payment', ?, ?, ?)""",
                (date, account_id, -amount, f"Règlement {item['name']}", cur.lastrowid, g.user["id"], now_utc()),
            )
        log_activity("Règlement fournisseur", f"{item['name']} : {fmt_money(amount)}")
        flash(f"Règlement de {fmt_money(amount)} enregistré.", "success")
    return redirect(url_for("matieres.supplier", supplier_id=supplier_id))


@bp.route("/reglements/<int:payment_id>/corriger", methods=["POST"])
@require("matieres", EDIT)
def payment_edit(payment_id):
    payment = query("SELECT * FROM supplier_payments WHERE id = ?", (payment_id,), one=True)
    if payment is None:
        abort(404)
    if not can_correct("matieres", payment):
        abort(403)
    back = redirect(url_for("matieres.supplier", supplier_id=payment["supplier_id"]))
    amount = parse_num(request.form.get("amount"))
    date = request.form.get("date", payment["date"])
    account = query("SELECT * FROM accounts WHERE id = ?", (request.form.get("account_id", type=int) or 0,), one=True)
    max_amount = supplier_balance(payment["supplier_id"])["due"] + payment["amount"]
    if amount is None or amount <= 0:
        flash("Indiquez un montant supérieur à 0.", "error")
    elif amount > max_amount + 0.5:
        flash(f"Le montant dépasse la dette ({fmt_money(max_amount)}).", "error")
    elif account is None or not valid_date(date):
        flash("Compte ou date invalide.", "error")
    else:
        supplier = _supplier(payment["supplier_id"])
        with transaction() as conn:
            conn.execute("UPDATE supplier_payments SET amount = ?, date = ?, account_id = ?, notes = ? WHERE id = ?",
                         (amount, date, account["id"], request.form.get("notes", "").strip(), payment_id))
            conn.execute("DELETE FROM cash_movements WHERE ref_type = 'supplier_payment' AND ref_id = ?", (payment_id,))
            conn.execute(
                """INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                   VALUES (?, ?, ?, 'reglement_fournisseur', ?, 'supplier_payment', ?, ?, ?)""",
                (date, account["id"], -amount, f"Règlement {supplier['name']}", payment_id, g.user["id"], now_utc()),
            )
        log_activity("Règlement fournisseur corrigé", f"{supplier['name']} : {fmt_money(payment['amount'])} → {fmt_money(amount)}")
        notify("supplier", supplier["id"], f"✏️ Règlement corrigé chez {supplier['name']} : "
               f"{fmt_money(payment['amount'])} → {fmt_money(amount)}")
        flash("Règlement corrigé.", "success")
    return back


@bp.route("/reglements/<int:payment_id>/supprimer", methods=["POST"])
@require("matieres", MANAGE)
def payment_delete(payment_id):
    payment = query("SELECT * FROM supplier_payments WHERE id = ?", (payment_id,), one=True)
    if payment is None:
        abort(404)
    with transaction() as conn:
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'supplier_payment' AND ref_id = ?", (payment_id,))
        conn.execute("DELETE FROM supplier_payments WHERE id = ?", (payment_id,))
    log_activity("Règlement fournisseur annulé", fmt_money(payment["amount"]))
    notify("supplier", payment["supplier_id"], f"↩️ Règlement annulé : {fmt_money(payment['amount'])} du {date_fr(payment['date'])}")
    flash("Règlement annulé.", "success")
    return redirect(url_for("matieres.supplier", supplier_id=payment["supplier_id"]))


# ---------------------------------------------------------------------------
# Achats
# ---------------------------------------------------------------------------
@bp.route("/achats")
@require("matieres", VIEW)
def purchases():
    start = request.args.get("du", "")
    end = request.args.get("au", "")
    supplier_id = request.args.get("fournisseur", "")
    where, params = [], []
    if valid_date(start):
        where.append("p.date >= ?")
        params.append(start)
    if valid_date(end):
        where.append("p.date <= ?")
        params.append(end)
    if supplier_id:
        where.append("p.supplier_id = ?")
        params.append(supplier_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = query(
        f"""SELECT p.*, s.name AS supplier, a.name AS account,
                   (SELECT GROUP_CONCAT(m.name, ', ') FROM purchase_lines l JOIN materials m ON m.id = l.material_id
                    WHERE l.purchase_id = p.id) AS items
            FROM purchases p LEFT JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN accounts a ON a.id = p.account_id
            {clause} ORDER BY p.date DESC, p.id DESC LIMIT 300""",
        params,
    )
    totals = {"total": sum(r["total"] for r in rows), "paid": sum(r["paid"] for r in rows)}
    return render_template("matieres/purchases.html", rows=rows, totals=totals, start=start, end=end,
                           supplier_id=supplier_id, suppliers=_suppliers(active_only=False))


def _purchase_form_from_request(materials):
    """Lit et vérifie le formulaire d'achat. Retourne (form, lignes, achat, erreurs)."""
    keys = ("date", "supplier_id", "reference", "transport_cost", "paid", "account_id", "notes", "pay_mode")
    form = {k: request.form.get(k, "").strip() for k in keys}
    lines, parsed, errors = [], [], []
    material_map = {str(m["id"]): m for m in materials}
    for index, (mid, q, p) in enumerate(zip(request.form.getlist("material_id"), request.form.getlist("quantity"),
                                            request.form.getlist("unit_price")), start=1):
        lines.append({"material_id": mid, "quantity": q, "unit_price": p})
        if not mid and not q and not p:
            continue
        qty, price = parse_num(q), parse_num(p, 0)
        if mid not in material_map:
            errors.append(f"Ligne {index} : choisissez une matière.")
        elif qty is None or qty <= 0:
            errors.append(f"Ligne {index} : la quantité doit être supérieure à 0.")
        elif price is None or price < 0:
            errors.append(f"Ligne {index} : prix invalide.")
        else:
            parsed.append((material_map[mid], qty, price))
    if not parsed and not errors:
        errors.append("Ajoutez au moins une matière achetée.")
    if not valid_date(form["date"]):
        errors.append("Date invalide.")
    transport = parse_num(form["transport_cost"], 0) or 0
    if transport < 0:
        errors.append("Les frais de transport ne peuvent pas être négatifs.")
    goods = sum(q * p for _, q, p in parsed)
    total = goods + transport
    if form["pay_mode"] == "credit":
        paid = 0
    elif form["pay_mode"] == "partiel":
        paid = parse_num(form["paid"], 0) or 0
    else:
        paid = total
    if paid < 0 or paid > total + 0.5:
        errors.append("Le montant payé doit être entre 0 et le total de l'achat.")
    account = query("SELECT * FROM accounts WHERE id = ?", (form["account_id"] or 0,), one=True)
    if paid > 0 and account is None:
        errors.append("Choisissez d'où vient l'argent (caisse ou propriétaire).")
    # Fournisseur écrit librement : on retrouve celui qui porte ce nom, sinon on le créera
    typed = " ".join(request.form.get("supplier_name", "").split())[:80]
    form["supplier_name"] = typed
    supplier = None
    new_supplier = False
    if "supplier_name" in request.form:
        if typed:
            supplier = query("SELECT * FROM suppliers WHERE name = ? COLLATE NOCASE", (typed,), one=True)
            new_supplier = supplier is None
            if new_supplier and len(typed) < 2:
                errors.append("Le nom du fournisseur est trop court.")
    elif form["supplier_id"]:
        supplier = query("SELECT * FROM suppliers WHERE id = ?", (form["supplier_id"],), one=True)
    if paid < total - 0.5 and supplier is None and not new_supplier:
        errors.append("Un achat à crédit doit avoir un fournisseur (pour suivre la dette).")
    if new_supplier and not errors:
        supplier_id = execute("INSERT INTO suppliers (name, phone, address, notes, created_at) VALUES (?, '', '', '', ?)",
                              (typed, now_utc()))
        log_activity("Fournisseur ajouté", typed)
        supplier = query("SELECT * FROM suppliers WHERE id = ?", (supplier_id,), one=True)
    form["supplier_id"] = str(supplier["id"]) if supplier else ""
    data = {"parsed": parsed, "transport": transport, "goods": goods, "total": total, "paid": paid,
            "account": account, "supplier": supplier}
    return form, lines or [{"material_id": "", "quantity": "", "unit_price": ""}], data, errors


def _write_purchase(conn, purchase_id, form, data):
    """Écrit les lignes, mouvements de stock et de caisse d'un achat."""
    parsed, transport, goods = data["parsed"], data["transport"], data["goods"]
    supplier, account = data["supplier"], data["account"]
    for item, qty, price in parsed:
        line_total = qty * price
        share = (transport * line_total / goods) if goods > 0 else transport / len(parsed)
        conn.execute("INSERT INTO purchase_lines (purchase_id, material_id, quantity, unit_price, total) VALUES (?, ?, ?, ?, ?)",
                     (purchase_id, item["id"], qty, price, line_total))
        conn.execute(
            """INSERT INTO stock_moves (date, material_id, quantity, unit_cost, kind, ref_type, ref_id, created_by, created_at)
               VALUES (?, ?, ?, ?, 'achat', 'purchase', ?, ?, ?)""",
            (form["date"], item["id"], qty, (line_total + share) / qty, purchase_id, g.user["id"], now_utc()),
        )
    if data["paid"] > 0:
        label = "Achat matières" + (f" — {supplier['name']}" if supplier else "")
        conn.execute(
            """INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
               VALUES (?, ?, ?, 'achat', ?, 'purchase', ?, ?, ?)""",
            (form["date"], account["id"], -data["paid"], label, purchase_id, g.user["id"], now_utc()),
        )


@bp.route("/achats/nouveau", methods=["GET", "POST"])
@require("matieres", EDIT)
def purchase_new():
    materials = query("SELECT * FROM materials WHERE active = 1 ORDER BY name")
    form = {"date": today(), "supplier_id": request.args.get("fournisseur", ""), "reference": "",
            "transport_cost": "", "paid": "", "account_id": "", "notes": "", "pay_mode": "tout"}
    lines = [{"material_id": request.args.get("matiere", ""), "quantity": "", "unit_price": ""}]
    if request.method == "POST":
        form, lines, data, errors = _purchase_form_from_request(materials)
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            with transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO purchases (date, supplier_id, reference, transport_cost, total, paid, account_id,
                           notes, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (form["date"], data["supplier"]["id"] if data["supplier"] else None, form["reference"],
                     data["transport"], data["total"], data["paid"],
                     data["account"]["id"] if data["account"] and data["paid"] > 0 else None,
                     form["notes"], g.user["id"], now_utc()),
                )
                purchase_id = cur.lastrowid
                _write_purchase(conn, purchase_id, form, data)
                for material_id in {item["id"] for item, _, _ in data["parsed"]}:
                    recompute_material(conn, material_id)
            log_activity("Achat enregistré", f"n°{purchase_id} — {fmt_money(data['total'])}")
            flash(f"Achat enregistré ({fmt_money(data['total'])}). Le stock a été mis à jour.", "success")
            return redirect(url_for("matieres.purchase", purchase_id=purchase_id))
    return render_template("matieres/purchase_form.html", form=form, lines=lines, materials=materials,
                           suppliers=_suppliers(), accounts=accounts(), editing=None)


@bp.route("/achats/<int:purchase_id>/corriger", methods=["GET", "POST"])
@require("matieres", EDIT)
def purchase_edit(purchase_id):
    item = query("SELECT * FROM purchases WHERE id = ?", (purchase_id,), one=True)
    if item is None:
        abort(404)
    if not can_correct("matieres", item):
        abort(403)
    old_lines = query("""SELECT l.*, m.name, m.unit FROM purchase_lines l JOIN materials m ON m.id = l.material_id
                         WHERE l.purchase_id = ? ORDER BY l.id""", (purchase_id,))
    used_ids = {l["material_id"] for l in old_lines}
    materials = query(f"""SELECT * FROM materials WHERE active = 1 OR id IN ({",".join("?" * len(used_ids)) or "0"})
                          ORDER BY name""", tuple(used_ids))
    if item["paid"] >= item["total"] - 0.5:
        mode = "tout"
    elif item["paid"] <= 0:
        mode = "credit"
    else:
        mode = "partiel"
    form = {"date": item["date"], "supplier_id": str(item["supplier_id"] or ""), "reference": item["reference"],
            "transport_cost": fmt_qty(item["transport_cost"]) if item["transport_cost"] else "",
            "paid": fmt_qty(item["paid"]) if mode == "partiel" else "", "account_id": str(item["account_id"] or ""),
            "notes": item["notes"], "pay_mode": mode}
    lines = [{"material_id": str(l["material_id"]), "quantity": fmt_qty(l["quantity"]), "unit_price": fmt_qty(l["unit_price"])}
             for l in old_lines]
    if request.method == "POST":
        form, lines, data, errors = _purchase_form_from_request(materials)
        old_qty, new_qty = {}, {}
        for l in old_lines:
            old_qty[l["material_id"]] = old_qty.get(l["material_id"], 0) + l["quantity"]
        for m, q, _ in data["parsed"]:
            new_qty[m["id"]] = new_qty.get(m["id"], 0) + q
        for material_id in set(old_qty) | set(new_qty):
            after = material_stock(material_id) - old_qty.get(material_id, 0) + new_qty.get(material_id, 0)
            if after < -EPS:
                name = query("SELECT name, unit FROM materials WHERE id = ?", (material_id,), one=True)
                errors.append(f"Impossible : « {name['name']} » a déjà été utilisé, le stock deviendrait "
                              f"{fmt_qty(after, name['unit'])}.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            changes = []
            for l in old_lines:
                match = next((x for x in data["parsed"] if x[0]["id"] == l["material_id"]), None)
                if match is None:
                    changes.append(f"{l['name']} retiré")
                else:
                    if abs(match[1] - l["quantity"]) > EPS:
                        changes.append(f"{l['name']} {fmt_qty(l['quantity'], l['unit'])} → {fmt_qty(match[1], l['unit'])}")
                    if abs(match[2] - l["unit_price"]) > EPS:
                        changes.append(f"{l['name']} prix {fmt_money(l['unit_price'])} → {fmt_money(match[2])}")
            for m, q, _ in data["parsed"]:
                if m["id"] not in old_qty:
                    changes.append(f"{m['name']} ajouté ({fmt_qty(q, m['unit'])})")
            if abs(data["total"] - item["total"]) > 0.5:
                changes.append(f"total {fmt_money(item['total'])} → {fmt_money(data['total'])}")
            if abs(data["paid"] - item["paid"]) > 0.5:
                changes.append(f"payé {fmt_money(item['paid'])} → {fmt_money(data['paid'])}")
            if form["date"] != item["date"]:
                changes.append(f"date {date_fr(item['date'])} → {date_fr(form['date'])}")
            with transaction() as conn:
                conn.execute("DELETE FROM stock_moves WHERE ref_type = 'purchase' AND ref_id = ?", (purchase_id,))
                conn.execute("DELETE FROM cash_movements WHERE ref_type = 'purchase' AND ref_id = ?", (purchase_id,))
                conn.execute("DELETE FROM purchase_lines WHERE purchase_id = ?", (purchase_id,))
                conn.execute(
                    """UPDATE purchases SET date = ?, supplier_id = ?, reference = ?, transport_cost = ?, total = ?,
                           paid = ?, account_id = ?, notes = ? WHERE id = ?""",
                    (form["date"], data["supplier"]["id"] if data["supplier"] else None, form["reference"],
                     data["transport"], data["total"], data["paid"],
                     data["account"]["id"] if data["account"] and data["paid"] > 0 else None, form["notes"], purchase_id),
                )
                _write_purchase(conn, purchase_id, form, data)
                for material_id in set(old_qty) | set(new_qty):
                    recompute_material(conn, material_id)
            log_activity("Achat corrigé", f"n°{purchase_id} : " + ("; ".join(changes) or "aucun changement de chiffre"))
            if changes:
                notify("purchase", purchase_id, f"✏️ Achat n°{purchase_id} corrigé : " + "; ".join(changes))
            flash("Achat corrigé. Stock, prix moyen, caisse et dette fournisseur ont été recalculés.", "success")
            return redirect(url_for("matieres.purchase", purchase_id=purchase_id))
    return render_template("matieres/purchase_form.html", form=form, lines=lines or [{"material_id": "", "quantity": "",
                           "unit_price": ""}], materials=materials, suppliers=_suppliers(active_only=False),
                           accounts=accounts(active_only=False), editing=item)


@bp.route("/achats/<int:purchase_id>")
@require("matieres", VIEW)
def purchase(purchase_id):
    item = query("""SELECT p.*, s.name AS supplier, a.name AS account, u.username
                    FROM purchases p LEFT JOIN suppliers s ON s.id = p.supplier_id
                    LEFT JOIN accounts a ON a.id = p.account_id LEFT JOIN users u ON u.id = p.created_by
                    WHERE p.id = ?""", (purchase_id,), one=True)
    if item is None:
        abort(404)
    rows = query("""SELECT l.*, m.name, m.unit FROM purchase_lines l JOIN materials m ON m.id = l.material_id
                    WHERE l.purchase_id = ? ORDER BY l.id""", (purchase_id,))
    goods = sum(r["total"] for r in rows)
    lines = []
    for r in rows:
        line = dict(r)
        share = (item["transport_cost"] * r["total"] / goods) if goods > 0 else (item["transport_cost"] / len(rows))
        line["unit_cost"] = (r["total"] + share) / r["quantity"] if r["quantity"] else 0
        lines.append(line)
    return render_template("matieres/purchase.html", item=item, lines=lines)


@bp.route("/achats/<int:purchase_id>/supprimer", methods=["POST"])
@require("matieres", MANAGE)
def purchase_delete(purchase_id):
    item = query("SELECT * FROM purchases WHERE id = ?", (purchase_id,), one=True)
    if item is None:
        abort(404)
    moves = query("SELECT * FROM stock_moves WHERE ref_type = 'purchase' AND ref_id = ?", (purchase_id,))
    per_material = {}
    for move in moves:
        per_material[move["material_id"]] = per_material.get(move["material_id"], 0) + move["quantity"]
    for material_id, qty in per_material.items():
        if material_stock(material_id) - qty < -EPS:
            name = query("SELECT name FROM materials WHERE id = ?", (material_id,), one=True)["name"]
            flash(f"Impossible d'annuler : une partie de « {name} » a déjà été utilisée "
                  "(le stock deviendrait négatif).", "error")
            return redirect(url_for("matieres.purchase", purchase_id=purchase_id))
    with transaction() as conn:
        conn.execute("DELETE FROM stock_moves WHERE ref_type = 'purchase' AND ref_id = ?", (purchase_id,))
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'purchase' AND ref_id = ?", (purchase_id,))
        conn.execute("DELETE FROM purchase_lines WHERE purchase_id = ?", (purchase_id,))
        conn.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
        for material_id in per_material:
            recompute_material(conn, material_id)
    log_activity("Achat annulé", f"n°{purchase_id} — {fmt_money(item['total'])}")
    notify("", None, f"↩️ Achat n°{purchase_id} du {date_fr(item['date'])} annulé ({fmt_money(item['total'])}) : "
           "stock et caisse corrigés.")
    flash("Achat annulé : le stock et la caisse ont été corrigés.", "success")
    return redirect(url_for("matieres.purchases"))
