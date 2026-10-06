"""Tableau de bord, pages des modules à venir, thème et journal d'activité."""
from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, url_for

from .db import execute, query
from .security import MODULE_ENDPOINTS, MODULE_INFO, MODULES, admin_required, can, is_admin, login_required, role_label
from .utils import local_now

bp = Blueprint("main", __name__)


@bp.route("/")
@login_required
def dashboard():
    hour = local_now().hour
    greeting = "Bonjour" if hour < 18 else "Bonsoir"
    modules = [MODULE_INFO[m[0]] for m in MODULES if m[0] != "dashboard" and can(m[0])]
    stats = None
    if is_admin():
        stats = {
            "users": query("SELECT COUNT(*) AS n FROM users WHERE deleted = 0 AND active = 1", one=True)["n"],
            "inactive": query("SELECT COUNT(*) AS n FROM users WHERE deleted = 0 AND active = 0", one=True)["n"],
            "activity": query("SELECT * FROM activity_log ORDER BY id DESC LIMIT 8"),
        }
        from .backups import list_backups

        backups = list_backups()
        stats["last_backup"] = backups[0] if backups else None
    return render_template("main/dashboard.html", greeting=greeting, modules=modules, stats=stats,
                           role=role_label(g.user["role"]))


@bp.route("/module/<key>")
@login_required
def module(key):
    info = MODULE_INFO.get(key)
    if info is None:
        abort(404)
    if key in MODULE_ENDPOINTS:
        return redirect(url_for(MODULE_ENDPOINTS[key]))
    if not can(key):
        abort(403)
    return render_template("main/coming_soon.html", info=info)


@bp.route("/preferences/theme", methods=["POST"])
@login_required
def set_theme():
    theme = request.form.get("theme") or (request.get_json(silent=True) or {}).get("theme")
    if theme not in ("light", "dark", "auto"):
        return jsonify(ok=False), 400
    execute("UPDATE users SET theme = ? WHERE id = ?", (theme, g.user["id"]))
    return jsonify(ok=True, theme=theme)


@bp.route("/journal")
@admin_required
def journal():
    page = max(1, request.args.get("page", 1, type=int))
    who = request.args.get("utilisateur", "").strip()
    start = request.args.get("du", "").strip()
    end = request.args.get("au", "").strip()
    where, params = [], []
    if who:
        where.append("username = ?")
        params.append(who)
    # Les dates saisies sont à l'heure de Madagascar (UTC+3) ; la base est en UTC.
    if start:
        where.append("datetime(created_at, '+3 hours') >= ?")
        params.append(f"{start} 00:00:00")
    if end:
        where.append("datetime(created_at, '+3 hours') <= ?")
        params.append(f"{end} 23:59:59")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    per_page = 50
    total = query(f"SELECT COUNT(*) AS n FROM activity_log {clause}", params, one=True)["n"]
    rows = query(f"SELECT * FROM activity_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                 [*params, per_page, (page - 1) * per_page])
    names = [r["username"] for r in query("SELECT DISTINCT username FROM activity_log WHERE username != '' ORDER BY username")]
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template("main/journal.html", rows=rows, page=page, pages=pages, total=total,
                           names=names, who=who, start=start, end=end)
