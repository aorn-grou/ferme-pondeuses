"""Gestion des utilisateurs : création, rôles, droits, désactivation, suppression."""
import json
import secrets

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from .db import execute, now_utc, query
from .security import (
    ASSIGNABLE, LEVEL_LABELS, MODULE_INFO, ROLE_DEFAULTS, ROLES, admin_required,
    is_super_admin, password_problem, role_label, user_levels,
)
from .utils import log_activity

bp = Blueprint("users", __name__, url_prefix="/utilisateurs")

WORDS = ["Poule", "Oeuf", "Ferme", "Mais", "Soja", "Coq", "Lot", "Nid", "Grain", "Ponte"]


def temp_password():
    return f"{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"


def _get(user_id):
    user = query("SELECT * FROM users WHERE id = ? AND deleted = 0", (user_id,), one=True)
    if user is None:
        abort(404)
    return user


def _admin_count():
    return query("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND deleted = 0", one=True)["n"]


def can_edit(target):
    """Le Super-admin ne peut être modifié que par lui-même."""
    if target["role"] == "super_admin":
        return is_super_admin()
    return True


def can_delete(target):
    """Règle demandée : on ne supprime ni le Super-admin ni un Admin.
    Seul le Super-admin peut retirer un Admin, et jamais le dernier."""
    if target["id"] == g.user["id"] or target["role"] == "super_admin":
        return False
    if target["role"] == "admin":
        return is_super_admin() and _admin_count() > 1
    return True


def assignable_roles(target=None):
    roles = {k: v for k, v in ROLES.items() if k != "super_admin"}
    if target is not None and target["role"] == "super_admin":
        return {"super_admin": ROLES["super_admin"]}
    return roles


def _perms_from_form():
    if request.form.get("use_defaults"):
        return ""
    levels = {}
    for key in ASSIGNABLE:
        try:
            level = int(request.form.get(f"perm_{key}", 0))
        except ValueError:
            level = 0
        levels[key] = max(0, min(3, level))
    return json.dumps(levels)


@bp.route("/")
@admin_required
def index():
    show = request.args.get("voir", "actifs")
    clause = "deleted = 0" + (" AND active = 1" if show == "actifs" else " AND active = 0" if show == "inactifs" else "")
    users = query(f"SELECT * FROM users WHERE {clause} ORDER BY CASE role WHEN 'super_admin' THEN 0 "
                  "WHEN 'admin' THEN 1 ELSE 2 END, username")
    return render_template("users/index.html", users=users, show=show, can_delete=can_delete, can_edit=can_edit)


@bp.route("/nouveau", methods=["GET", "POST"])
@admin_required
def create():
    form = {"username": "", "full_name": "", "email": "", "phone": "", "role": "elevage"}
    suggested = temp_password()
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        password = request.form.get("password", "").strip()
        errors = []
        if len(form["username"]) < 3 or " " in form["username"]:
            errors.append("Le nom d'utilisateur doit faire au moins 3 caractères, sans espace.")
        elif query("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (form["username"],), one=True):
            errors.append("Ce nom d'utilisateur existe déjà.")
        if form["role"] not in assignable_roles():
            errors.append("Rôle invalide.")
        if form["email"] and "@" not in form["email"]:
            errors.append("L'adresse e-mail n'est pas valide.")
        if password_problem(password):
            errors.append("Mot de passe provisoire : " + password_problem(password))
        if errors:
            for message in errors:
                flash(message, "error")
            suggested = password or suggested
        else:
            perms = _perms_from_form() if form["role"] not in ("admin",) else ""
            user_id = execute(
                """INSERT INTO users (username, full_name, email, phone, role, password_hash, perms,
                       must_change_password, setup_done, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)""",
                (form["username"], form["full_name"], form["email"], form["phone"], form["role"],
                 generate_password_hash(password), perms, now_utc()),
            )
            log_activity("Utilisateur créé", f"{form['username']} ({role_label(form['role'])})")
            flash(f"Compte « {form['username']} » créé. Mot de passe provisoire : {password} — "
                  "communiquez-le à la personne ; elle devra le changer à sa première connexion.", "success")
            return redirect(url_for("users.edit", user_id=user_id))
    return render_template("users/form.html", form=form, user=None, roles=assignable_roles(),
                           suggested=suggested, perm_rows=_perm_rows(None, form["role"]),
                           role_defaults=_role_defaults_json())


def _perm_rows(user, role):
    if user is not None:
        levels = user_levels(user)
        custom = bool(user["perms"])
    else:
        levels = dict(ROLE_DEFAULTS.get(role, {}))
        custom = False
    rows = [{"key": k, "label": MODULE_INFO[k]["label"], "group": MODULE_INFO[k]["group"],
             "level": levels.get(k, 0)} for k in ASSIGNABLE]
    return {"rows": rows, "custom": custom}


def _role_defaults_json():
    return json.dumps({role: {k: ROLE_DEFAULTS.get(role, {}).get(k, 0) for k in ASSIGNABLE} for role in ROLES})


@bp.route("/<int:user_id>", methods=["GET", "POST"])
@admin_required
def edit(user_id):
    user = _get(user_id)
    if not can_edit(user):
        abort(403)
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        role = request.form.get("role", user["role"])
        if role not in assignable_roles(user):
            flash("Rôle invalide.", "error")
        elif email and "@" not in email:
            flash("L'adresse e-mail n'est pas valide.", "error")
        elif user["id"] == g.user["id"] and role != user["role"]:
            flash("Vous ne pouvez pas changer votre propre rôle.", "error")
        elif user["role"] == "admin" and role != "admin" and not is_super_admin():
            flash("Seul le Super-admin peut retirer le rôle Administrateur.", "error")
        elif user["role"] == "admin" and role != "admin" and _admin_count() <= 1:
            flash("Il faut garder au moins un Administrateur.", "error")
        else:
            perms = "" if role in ("admin", "super_admin") else _perms_from_form()
            execute("UPDATE users SET full_name = ?, email = ?, phone = ?, role = ?, perms = ? WHERE id = ?",
                    (full_name, email, phone, role, perms, user["id"]))
            log_activity("Utilisateur modifié", f"{user['username']} ({role_label(role)})")
            flash("Modifications enregistrées.", "success")
            return redirect(url_for("users.edit", user_id=user["id"]))
        user = _get(user_id)
    return render_template("users/form.html", form=user, user=user, roles=assignable_roles(user),
                           suggested=temp_password(), perm_rows=_perm_rows(user, user["role"]),
                           role_defaults=_role_defaults_json(), can_delete=can_delete(user),
                           level_labels=LEVEL_LABELS)


@bp.route("/<int:user_id>/mot-de-passe", methods=["POST"])
@admin_required
def reset_password(user_id):
    user = _get(user_id)
    if not can_edit(user):
        abort(403)
    password = request.form.get("password", "").strip()
    if password_problem(password):
        flash(password_problem(password), "error")
    else:
        execute(
            """UPDATE users SET password_hash = ?, must_change_password = 1, failed_attempts = 0,
                   locked_until = '' WHERE id = ?""",
            (generate_password_hash(password), user["id"]),
        )
        log_activity("Mot de passe réinitialisé par l'administrateur", user["username"])
        flash(f"Nouveau mot de passe provisoire de « {user['username']} » : {password} — "
              "il devra le changer à sa prochaine connexion.", "success")
    return redirect(url_for("users.edit", user_id=user["id"]))


@bp.route("/<int:user_id>/activer", methods=["POST"])
@admin_required
def toggle_active(user_id):
    user = _get(user_id)
    if user["id"] == g.user["id"] or user["role"] == "super_admin":
        flash("Ce compte ne peut pas être désactivé.", "error")
    elif user["role"] == "admin" and not is_super_admin():
        flash("Seul le Super-admin peut désactiver un Administrateur.", "error")
    else:
        new_state = 0 if user["active"] else 1
        execute("UPDATE users SET active = ?, failed_attempts = 0, locked_until = '' WHERE id = ?",
                (new_state, user["id"]))
        log_activity("Utilisateur " + ("réactivé" if new_state else "désactivé"), user["username"])
        flash(f"Compte « {user['username']} » " + ("réactivé." if new_state else "désactivé."), "success")
    return redirect(request.referrer or url_for("users.index"))


@bp.route("/<int:user_id>/supprimer", methods=["POST"])
@admin_required
def delete(user_id):
    user = _get(user_id)
    if not can_delete(user):
        flash("Ce compte ne peut pas être supprimé.", "error")
        return redirect(url_for("users.edit", user_id=user["id"]))
    if request.form.get("confirm_name", "").strip() != user["username"]:
        flash("Pour confirmer, tapez exactement le nom d'utilisateur.", "error")
        return redirect(url_for("users.edit", user_id=user["id"]))
    # Suppression « douce » : l'historique garde ses saisies, le nom redevient libre.
    execute(
        "UPDATE users SET deleted = 1, active = 0, username = username || '#suppr' || id WHERE id = ?",
        (user["id"],),
    )
    log_activity("Utilisateur supprimé", user["username"])
    flash(f"Compte « {user['username']} » supprimé. Ses saisies passées restent dans l'historique.", "success")
    return redirect(url_for("users.index"))
