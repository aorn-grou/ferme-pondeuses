"""Logiciel de gestion de ferme de poules pondeuses."""
import os
import secrets
from datetime import datetime, timezone

from flask import Flask, flash, g, redirect, render_template, request, session, url_for

from . import db
from .security import (
    LEVEL_LABELS, MENU_GROUPS, MODULE_ENDPOINTS, MODULE_INFO, MODULES, can, can_correct, check_csrf, csrf_token,
    is_admin, is_super_admin, role_label,
)
from .utils import (
    all_settings, date_fr, fmt_money, fmt_number, fmt_qty, local_datetime, readable_text_on, valid_hex,
)

VERSION = "0.4.0"


def _secret_key(instance_dir):
    """Clé secrète créée une fois et gardée hors de GitHub (dossier instance)."""
    path = os.path.join(instance_dir, "secret_key.txt")
    if os.environ.get("FERME_SECRET_KEY"):
        return os.environ["FERME_SECRET_KEY"]
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32))
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def create_app(test_config=None):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    instance_dir = os.environ.get("FERME_DATA_DIR", os.path.join(base_dir, "instance"))
    os.makedirs(instance_dir, exist_ok=True)

    app = Flask(__name__, instance_path=instance_dir)
    app.config.update(
        DATABASE=os.path.join(instance_dir, "ferme.db"),
        BACKUP_DIR=os.path.join(instance_dir, "backups"),
        UPLOAD_DIR=os.path.join(instance_dir, "uploads"),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        AUTO_BACKUP_KEEP=30,
    )
    if test_config:
        app.config.update(test_config)
    app.secret_key = app.config.get("SECRET_KEY") or _secret_key(instance_dir)
    os.makedirs(app.config["BACKUP_DIR"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    db.init_app(app)

    from . import auth, backups, discussion, main, matieres, provenderie, settings, users

    app.register_blueprint(discussion.bp)

    app.register_blueprint(matieres.bp)
    app.register_blueprint(provenderie.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(backups.bp)

    @app.before_request
    def before_request():
        if request.endpoint == "static":
            return None
        check_csrf()
        g.user = None
        user_id = session.get("user_id")
        if user_id:
            user = db.query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
            if user is None or not user["active"] or user["deleted"]:
                session.clear()
                flash("Votre session a été fermée.", "info")
                return redirect(url_for("auth.login"))
            # Déconnexion automatique après inactivité
            timeout = int(all_settings().get("session_timeout") or 30)
            now = datetime.now(timezone.utc).timestamp()
            last_seen = session.get("last_seen", now)
            if timeout > 0 and now - last_seen > timeout * 60:
                session.clear()
                flash("Vous avez été déconnecté après une période d'inactivité.", "info")
                return redirect(url_for("auth.login"))
            # Les vérifications automatiques (nouveaux messages) ne comptent pas comme une activité
            if request.endpoint not in ("discussion.counter", "discussion.newer"):
                session["last_seen"] = now
            g.user = user
            # Première connexion : mot de passe, e-mail et question secrète obligatoires
            allowed = {"auth.first_setup", "auth.logout", "main.set_theme", "settings.logo"}
            if (user["must_change_password"] or not user["setup_done"]) and request.endpoint not in allowed:
                return redirect(url_for("auth.first_setup"))
            backups.auto_backup_if_due()
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.context_processor
    def inject_globals():
        conf = all_settings()
        primary = valid_hex(conf.get("primary_color"), "#1d6b35")
        menu = valid_hex(conf.get("menu_color"), "#0e3b20")
        user = g.get("user")
        theme = (user["theme"] if user else None) or conf.get("default_theme") or "auto"
        if theme == "auto" and conf.get("default_theme") in ("light", "dark") and not user:
            theme = conf.get("default_theme")
        menu_groups = []
        for group in MENU_GROUPS:
            items = [MODULE_INFO[m[0]] for m in MODULES if m[3] == group and user and can(m[0])]
            if items:
                menu_groups.append((group, items))
        def module_url(key):
            endpoint = MODULE_ENDPOINTS.get(key)
            return url_for(endpoint) if endpoint else url_for("main.module", key=key)

        from .alerts import current_alerts
        from .discussion import comments_for, unread_count

        if conf.get("logo_file"):
            logo_url = url_for("settings.logo") + "?v=" + conf.get("logo_file", "")
        else:
            logo_url = url_for("static", filename="img/androfia-logo.png", v=VERSION)

        return {
            "logo_url": logo_url,
            "module_url": module_url,
            "unread_messages": unread_count() if user and not (user["must_change_password"] or not user["setup_done"]) else 0,
            "comments_for": comments_for,
            "alerts": current_alerts() if user else [],
            "conf": conf,
            "app_version": VERSION,
            "theme": theme,
            "primary_color": primary,
            "primary_text": readable_text_on(primary),
            "menu_color": menu,
            "menu_text": readable_text_on(menu),
            "menu_groups": menu_groups,
            "can": can,
            "can_correct": can_correct,
            "is_admin": is_admin,
            "is_super_admin": is_super_admin,
            "role_label": role_label,
            "csrf_token": csrf_token,
            "level_labels": LEVEL_LABELS,
            "current_user": user,
        }

    app.jinja_env.filters["localdt"] = local_datetime
    app.jinja_env.filters["money"] = fmt_money
    app.jinja_env.filters["num"] = fmt_number
    app.jinja_env.filters["qty"] = fmt_qty
    app.jinja_env.filters["date_fr"] = date_fr

    @app.errorhandler(400)
    def bad_request(error):
        return render_template("error.html", code=400, message=error.description), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "error.html", code=403,
            message="Vous n'avez pas l'autorisation d'ouvrir cette page. Demandez l'accès à l'administrateur.",
        ), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="Cette page n'existe pas."), 404

    @app.errorhandler(413)
    def too_large(_error):
        return render_template("error.html", code=413, message="Le fichier envoyé est trop gros."), 413

    return app
