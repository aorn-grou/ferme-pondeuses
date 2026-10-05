"""Paramètres : identité de la société, apparence, e-mail, sécurité."""
import os

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_from_directory, url_for,
)

from .mailer import send_email
from .security import admin_required
from .utils import get_setting, log_activity, set_setting, valid_hex

bp = Blueprint("settings", __name__, url_prefix="/parametres")

LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
COMPANY_FIELDS = ["company_name", "company_slogan", "company_address", "company_phone",
                  "company_email", "company_nif", "company_stat"]
COLOR_PRESETS = ["#2f7d4f", "#1f6feb", "#c2410c", "#7c3aed", "#0f766e", "#b91c1c", "#a16207", "#334155"]
MENU_PRESETS = ["#16302b", "#0f172a", "#1e293b", "#3b0764", "#422006", "#ffffff", "#f1f5f2", "#14532d"]


def _is_image(data, ext):
    signatures = {"png": [b"\x89PNG"], "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
                  "gif": [b"GIF87a", b"GIF89a"], "webp": [b"RIFF"]}
    return any(data.startswith(sig) for sig in signatures.get(ext, []))


@bp.route("/", methods=["GET", "POST"])
@admin_required
def index():
    tab = request.args.get("onglet", "societe")
    if request.method == "POST":
        section = request.form.get("section")
        if section == "societe":
            name = request.form.get("company_name", "").strip()
            if not name:
                flash("Le nom de la société est obligatoire.", "error")
                return redirect(url_for("settings.index", onglet="societe"))
            for field in COMPANY_FIELDS:
                set_setting(field, request.form.get(field, "").strip())
            upload = request.files.get("logo")
            if upload and upload.filename:
                ext = upload.filename.rsplit(".", 1)[-1].lower()
                data = upload.read()
                if ext not in LOGO_TYPES or not _is_image(data, ext):
                    flash("Logo refusé : utilisez une image PNG, JPG, WEBP ou GIF.", "error")
                elif len(data) > 2 * 1024 * 1024:
                    flash("Logo trop lourd (2 Mo maximum).", "error")
                else:
                    old = get_setting("logo_file")
                    filename = f"logo.{ext}"
                    if old and old != filename:
                        try:
                            os.remove(os.path.join(current_app.config["UPLOAD_DIR"], old))
                        except OSError:
                            pass
                    with open(os.path.join(current_app.config["UPLOAD_DIR"], filename), "wb") as handle:
                        handle.write(data)
                    set_setting("logo_file", filename)
            if request.form.get("remove_logo"):
                set_setting("logo_file", "")
            log_activity("Paramètres modifiés", "Identité de la société")
            flash("Informations de la société enregistrées.", "success")
        elif section == "apparence":
            set_setting("primary_color", valid_hex(request.form.get("primary_color"), "#2f7d4f"))
            set_setting("menu_color", valid_hex(request.form.get("menu_color"), "#16302b"))
            theme = request.form.get("default_theme", "auto")
            set_setting("default_theme", theme if theme in ("auto", "light", "dark") else "auto")
            set_setting("currency", request.form.get("currency", "Ar").strip()[:6] or "Ar")
            log_activity("Paramètres modifiés", "Apparence")
            flash("Apparence enregistrée.", "success")
        elif section == "email":
            for field in ("smtp_host", "smtp_port", "smtp_user", "smtp_from"):
                set_setting(field, request.form.get(field, "").strip())
            password = request.form.get("smtp_password", "")
            if password:
                set_setting("smtp_password", password.replace(" ", ""))
            if request.form.get("clear_password"):
                set_setting("smtp_password", "")
            log_activity("Paramètres modifiés", "E-mail")
            flash("Réglages e-mail enregistrés.", "success")
            if request.form.get("send_test"):
                target = g.user["email"] or get_setting("smtp_user")
                ok, error = send_email(target, "Test d'envoi", "Les e-mails du logiciel fonctionnent correctement.")
                flash(f"E-mail de test envoyé à {target}." if ok else error, "success" if ok else "error")
        elif section == "securite":
            try:
                minutes = max(0, min(480, int(request.form.get("session_timeout", 30))))
            except ValueError:
                minutes = 30
            set_setting("session_timeout", minutes)
            log_activity("Paramètres modifiés", "Sécurité")
            flash("Réglages de sécurité enregistrés.", "success")
        return redirect(url_for("settings.index", onglet=section or tab))
    return render_template("settings/index.html", tab=tab, color_presets=COLOR_PRESETS, menu_presets=MENU_PRESETS)


@bp.route("/logo")
def logo():
    filename = get_setting("logo_file")
    if not filename:
        abort(404)
    ext = filename.rsplit(".", 1)[-1]
    response = send_from_directory(current_app.config["UPLOAD_DIR"], filename, mimetype=LOGO_TYPES.get(ext))
    response.headers["Cache-Control"] = "no-cache"
    return response
