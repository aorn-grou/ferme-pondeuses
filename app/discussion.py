"""Discussion entre associés et commentaires attachés aux saisies (achats, fabrications…)."""
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .db import execute, now_utc, query
from .security import EDIT, VIEW, can, is_admin, login_required, require
from .utils import log_activity

bp = Blueprint("discussion", __name__, url_prefix="/discussion")

MAX_LENGTH = 2000

# Ce qu'on peut commenter : type -> (module qui donne le droit de voir, libellé, page)
REF_TYPES = {
    "purchase": ("matieres", "Achat n°{id}", "matieres.purchase", "purchase_id"),
    "material": ("matieres", "Matière « {name} »", "matieres.material", "material_id"),
    "supplier": ("matieres", "Fournisseur « {name} »", "matieres.supplier", "supplier_id"),
    "production": ("provenderie", "Fabrication n°{id}", "provenderie.production", "production_id"),
    "formula": ("provenderie", "Formule « {name} »", "provenderie.formula", "formula_id"),
    "lot": ("lots", "Lot « {name} »", "lots.lot", "lot_id"),
    "sale": ("ventes", "Vente n°{id}", "ventes.sale", "sale_id"),
    "client": ("ventes", "Client « {name} »", "ventes.client", "client_id"),
}
NAME_TABLES = {"material": "materials", "supplier": "suppliers", "formula": "formulas", "lot": "lots", "client": "clients"}


def notify(ref_type, ref_id, body):
    """Signalement automatique dans la Discussion (écart d'inventaire, perte, correction…),
    pour que les associés voient tout de suite ce qui a changé."""
    try:
        execute("INSERT INTO messages (user_id, body, ref_type, ref_id, auto, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (g.user["id"] if g.get("user") else None, body[:MAX_LENGTH], ref_type or "", ref_id, now_utc()))
    except Exception:  # un signalement ne doit jamais bloquer l'enregistrement
        pass


def ref_info(ref_type, ref_id):
    """Libellé et lien d'une saisie commentée, ou None si on ne peut pas la voir."""
    spec = REF_TYPES.get(ref_type)
    if spec is None or not can(spec[0], VIEW):
        return None
    name = ""
    if ref_type in NAME_TABLES:
        row = query(f"SELECT name FROM {NAME_TABLES[ref_type]} WHERE id = ?", (ref_id,), one=True)
        if row is None:
            return {"label": "Élément supprimé", "url": None}
        name = row["name"]
    return {"label": spec[1].format(id=ref_id, name=name), "url": url_for(spec[2], **{spec[3]: ref_id})}


def _visible_ref_types():
    return [t for t, spec in REF_TYPES.items() if can(spec[0], VIEW)]


def can_chat():
    return can("discussion", VIEW)


def unread_count(user=None):
    user = user or g.get("user")
    if user is None:
        return 0
    types = _visible_ref_types()
    placeholders = ",".join("?" * len(types))
    general = "(m.ref_type = '' OR m.ref_type IS NULL)" if can_chat() else "0"
    refs = f"m.ref_type IN ({placeholders})" if types else "0"
    row = query(f"""SELECT COUNT(*) AS n FROM messages m WHERE m.deleted = 0 AND m.id > ? AND m.user_id != ?
                    AND ({general} OR {refs})""", (user["last_seen_message"], user["id"], *types), one=True)
    return row["n"]


def comments_for(ref_type, ref_id):
    return query("""SELECT m.*, u.username, u.full_name FROM messages m LEFT JOIN users u ON u.id = m.user_id
                    WHERE m.ref_type = ? AND m.ref_id = ? AND m.deleted = 0 ORDER BY m.id""", (ref_type, ref_id))


def _feed(before=None, after=None, limit=60):
    """Messages visibles : discussion générale + commentaires des saisies qu'on peut voir."""
    types = _visible_ref_types()
    conds = []
    params = []
    if can_chat():
        conds.append("(m.ref_type = '' OR m.ref_type IS NULL)")
    if types:
        conds.append(f"m.ref_type IN ({','.join('?' * len(types))})")
        params.extend(types)
    if not conds:
        return []
    where = f"m.deleted = 0 AND ({' OR '.join(conds)})"
    if before:
        where += " AND m.id < ?"
        params.append(before)
    if after:
        where += " AND m.id > ?"
        params.append(after)
    rows = query(f"""SELECT m.*, u.username, u.full_name FROM messages m LEFT JOIN users u ON u.id = m.user_id
                     WHERE {where} ORDER BY m.id DESC LIMIT ?""", (*params, limit))
    items = []
    for row in reversed(rows):
        item = dict(row)
        item["ref"] = ref_info(row["ref_type"], row["ref_id"]) if row["ref_type"] else None
        items.append(item)
    return items


def _mark_seen(messages):
    if messages:
        last = max(m["id"] for m in messages)
        if last > g.user["last_seen_message"]:
            execute("UPDATE users SET last_seen_message = ? WHERE id = ?", (last, g.user["id"]))
            g.user = query("SELECT * FROM users WHERE id = ?", (g.user["id"],), one=True)


@bp.route("/")
@login_required
def index():
    if not can_chat() and not _visible_ref_types():
        abort(403)
    messages = _feed()
    previous_seen = g.user["last_seen_message"]
    _mark_seen(messages)
    oldest = messages[0]["id"] if messages else None
    has_more = bool(oldest and query("SELECT 1 FROM messages WHERE id < ? AND deleted = 0 LIMIT 1", (oldest,), one=True))
    return render_template("discussion/index.html", messages=messages, previous_seen=previous_seen,
                           can_write=can_chat(), has_more=has_more, max_length=MAX_LENGTH)


@bp.route("/plus-anciens")
@login_required
def older():
    before = request.args.get("avant", type=int)
    messages = _feed(before=before)
    return render_template("discussion/_messages.html", messages=messages, previous_seen=0)


@bp.route("/nouveaux")
@login_required
def newer():
    after = request.args.get("apres", type=int) or 0
    messages = _feed(after=after)
    if request.args.get("vu"):
        _mark_seen(messages)
    html = render_template("discussion/_messages.html", messages=messages, previous_seen=10 ** 12) if messages else ""
    return jsonify(html=html, last=messages[-1]["id"] if messages else after, unread=unread_count())


@bp.route("/compteur")
@login_required
def counter():
    return jsonify(unread=unread_count())


@bp.route("/envoyer", methods=["POST"])
@require("discussion", VIEW)
def send():
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Écrivez un message avant d'envoyer.", "error")
    elif len(body) > MAX_LENGTH:
        flash(f"Message trop long ({MAX_LENGTH} caractères maximum).", "error")
    else:
        message_id = execute("INSERT INTO messages (user_id, body, ref_type, created_at) VALUES (?, ?, '', ?)",
                             (g.user["id"], body, now_utc()))
        execute("UPDATE users SET last_seen_message = ? WHERE id = ? AND last_seen_message < ?",
                (message_id, g.user["id"], message_id))
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(ok=True)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=False, error="Message vide ou trop long."), 400
    return redirect(url_for("discussion.index") + "#bas")


@bp.route("/commentaire", methods=["POST"])
@login_required
def comment():
    ref_type = request.form.get("ref_type", "")
    ref_id = request.form.get("ref_id", type=int)
    body = (request.form.get("body") or "").strip()
    target = request.form.get("next") or url_for("discussion.index")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("discussion.index")
    if ref_type not in REF_TYPES or not ref_id or not can(REF_TYPES[ref_type][0], VIEW):
        abort(403)
    if not body:
        flash("Écrivez un commentaire avant d'envoyer.", "error")
    elif len(body) > MAX_LENGTH:
        flash(f"Commentaire trop long ({MAX_LENGTH} caractères maximum).", "error")
    else:
        message_id = execute("INSERT INTO messages (user_id, body, ref_type, ref_id, created_at) VALUES (?, ?, ?, ?, ?)",
                             (g.user["id"], body, ref_type, ref_id, now_utc()))
        execute("UPDATE users SET last_seen_message = ? WHERE id = ? AND last_seen_message < ?",
                (message_id, g.user["id"], message_id))
        flash("Commentaire ajouté. Vos associés le verront dans la Discussion.", "success")
    return redirect(target + "#commentaires")


@bp.route("/<int:message_id>/supprimer", methods=["POST"])
@login_required
def delete(message_id):
    message = query("SELECT * FROM messages WHERE id = ?", (message_id,), one=True)
    if message is None:
        abort(404)
    if message["user_id"] != g.user["id"] and not is_admin():
        abort(403)
    execute("UPDATE messages SET deleted = 1 WHERE id = ?", (message_id,))
    log_activity("Message supprimé", (message["body"] or "")[:80])
    target = request.form.get("next") or url_for("discussion.index")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("discussion.index")
    flash("Message supprimé.", "success")
    return redirect(target)


__all__ = ["bp", "comments_for", "unread_count", "EDIT"]
