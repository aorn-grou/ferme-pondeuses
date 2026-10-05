"""Sauvegardes automatiques et manuelles, restauration, transfert, réinitialisation."""
import os
import re
import sqlite3
from datetime import timedelta

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_from_directory, url_for,
)
from werkzeug.security import check_password_hash

from .db import close_db, connect, ensure_super_admin, is_valid_database, migrate
from .security import admin_required, super_admin_required
from .utils import get_setting, local_now, log_activity, set_setting

bp = Blueprint("backups", __name__, url_prefix="/sauvegardes")

NAME_RE = re.compile(r"^(auto|manuel|avant-restauration|avant-reinitialisation|importe)-\d{8}-\d{6}\.db$")
KIND_LABELS = {
    "auto": "Automatique",
    "manuel": "Manuelle",
    "avant-restauration": "Avant restauration",
    "avant-reinitialisation": "Avant réinitialisation",
    "importe": "Fichier importé",
}


def _backup_dir():
    return current_app.config["BACKUP_DIR"]


def create_backup(kind="manuel"):
    """Copie cohérente de la base (même pendant son utilisation)."""
    moment = local_now()
    name = f"{kind}-{moment.strftime('%Y%m%d-%H%M%S')}.db"
    path = os.path.join(_backup_dir(), name)
    while os.path.exists(path):  # deux sauvegardes dans la même seconde
        moment += timedelta(seconds=1)
        name = f"{kind}-{moment.strftime('%Y%m%d-%H%M%S')}.db"
        path = os.path.join(_backup_dir(), name)
    source = connect(current_app.config["DATABASE"])
    target = sqlite3.connect(path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return name


def list_backups():
    items = []
    for name in os.listdir(_backup_dir()):
        if not NAME_RE.match(name):
            continue
        path = os.path.join(_backup_dir(), name)
        kind = name.rsplit("-", 2)[0]
        stamp = name.rsplit("-", 2)[1] + name.rsplit("-", 2)[2][:6]
        items.append({
            "name": name,
            "kind": KIND_LABELS.get(kind, kind),
            "kind_key": kind,
            "date": f"{stamp[6:8]}/{stamp[4:6]}/{stamp[0:4]} {stamp[8:10]}:{stamp[10:12]}",
            "sort": stamp,
            "size_kb": max(1, os.path.getsize(path) // 1024),
        })
    return sorted(items, key=lambda item: item["sort"], reverse=True)


def _prune_auto(keep):
    autos = [b for b in list_backups() if b["kind_key"] == "auto"]
    for old in autos[keep:]:
        try:
            os.remove(os.path.join(_backup_dir(), old["name"]))
        except OSError:
            pass


def auto_backup_if_due():
    """Une sauvegarde automatique par jour, faite à la première visite de la journée."""
    today = local_now().strftime("%Y-%m-%d")
    if get_setting("last_auto_backup") == today:
        return
    try:
        create_backup("auto")
        set_setting("last_auto_backup", today)
        _prune_auto(current_app.config.get("AUTO_BACKUP_KEEP", 30))
    except (OSError, sqlite3.Error) as exc:
        current_app.logger.error("Sauvegarde automatique impossible : %s", exc)


def _safe_path(name):
    if not NAME_RE.match(name or ""):
        abort(404)
    path = os.path.join(_backup_dir(), name)
    if not os.path.exists(path):
        abort(404)
    return path


def restore_from(path):
    close_db()
    source = sqlite3.connect(path)
    target = connect(current_app.config["DATABASE"])
    try:
        source.backup(target)
        migrate(target)
        ensure_super_admin(target)
    finally:
        source.close()
        target.close()
    g.pop("settings", None)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@bp.route("/")
@admin_required
def index():
    return render_template("backups/index.html", backups=list_backups())


@bp.route("/creer", methods=["POST"])
@admin_required
def create():
    name = create_backup("manuel")
    log_activity("Sauvegarde manuelle", name)
    flash("Sauvegarde créée. Vous pouvez la télécharger pour la garder sur votre ordinateur ou téléphone.", "success")
    return redirect(url_for("backups.index"))


@bp.route("/telecharger/<name>")
@admin_required
def download(name):
    _safe_path(name)
    log_activity("Sauvegarde téléchargée", name)
    company = re.sub(r"[^A-Za-z0-9]+", "-", get_setting("company_name")).strip("-").lower() or "ferme"
    return send_from_directory(_backup_dir(), name, as_attachment=True, download_name=f"{company}-{name}")


@bp.route("/telecharger-maintenant")
@admin_required
def download_now():
    name = create_backup("manuel")
    log_activity("Sauvegarde téléchargée", name)
    return redirect(url_for("backups.download", name=name))


@bp.route("/restaurer/<name>", methods=["POST"])
@admin_required
def restore(name):
    path = _safe_path(name)
    if not check_password_hash(g.user["password_hash"], request.form.get("password", "")):
        flash("Mot de passe incorrect : restauration annulée.", "error")
        return redirect(url_for("backups.index"))
    if not is_valid_database(path):
        flash("Cette sauvegarde est abîmée ou n'est pas compatible.", "error")
        return redirect(url_for("backups.index"))
    safety = create_backup("avant-restauration")
    username = g.user["username"]
    restore_from(path)
    from .db import query

    user = query("SELECT * FROM users WHERE username = ? AND deleted = 0", (username,), one=True)
    log_activity("Sauvegarde restaurée", f"{name} (copie de sécurité : {safety})", user=user)
    flash(f"Données restaurées depuis la sauvegarde {name}. "
          f"Une copie de l'état précédent a été gardée ({safety}).", "success")
    return redirect(url_for("backups.index"))


@bp.route("/importer", methods=["POST"])
@admin_required
def upload():
    upload_file = request.files.get("fichier")
    if not upload_file or not upload_file.filename:
        flash("Choisissez un fichier de sauvegarde (.db).", "error")
        return redirect(url_for("backups.index"))
    if not check_password_hash(g.user["password_hash"], request.form.get("password", "")):
        flash("Mot de passe incorrect : restauration annulée.", "error")
        return redirect(url_for("backups.index"))
    name = f"importe-{local_now().strftime('%Y%m%d-%H%M%S')}.db"
    path = os.path.join(_backup_dir(), name)
    upload_file.save(path)
    if not is_valid_database(path):
        os.remove(path)
        flash("Ce fichier n'est pas une sauvegarde valide de ce logiciel.", "error")
        return redirect(url_for("backups.index"))
    safety = create_backup("avant-restauration")
    username = g.user["username"]
    restore_from(path)
    from .db import query

    user = query("SELECT * FROM users WHERE username = ? AND deleted = 0", (username,), one=True)
    log_activity("Sauvegarde importée et restaurée", f"{upload_file.filename} (copie de sécurité : {safety})", user=user)
    flash(f"Données restaurées depuis le fichier. Une copie de l'état précédent a été gardée ({safety}).", "success")
    return redirect(url_for("backups.index"))


@bp.route("/supprimer/<name>", methods=["POST"])
@admin_required
def delete(name):
    path = _safe_path(name)
    os.remove(path)
    log_activity("Sauvegarde supprimée", name)
    flash("Sauvegarde supprimée.", "success")
    return redirect(url_for("backups.index"))


@bp.route("/reinitialiser", methods=["POST"])
@super_admin_required
def factory_reset():
    if request.form.get("confirm_word", "").strip() != "EFFACER":
        flash("Tapez le mot EFFACER en majuscules pour confirmer.", "error")
        return redirect(url_for("backups.index"))
    if not check_password_hash(g.user["password_hash"], request.form.get("password", "")):
        flash("Mot de passe incorrect : réinitialisation annulée.", "error")
        return redirect(url_for("backups.index"))
    safety = create_backup("avant-reinitialisation")
    me = {k: g.user[k] for k in g.user.keys()}
    close_db()
    conn = connect(current_app.config["DATABASE"])
    try:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in tables:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.commit()
        migrate(conn)
        columns = [r["name"] for r in conn.execute('PRAGMA table_info("users")')]
        values = [me.get(c) for c in columns]
        conn.execute(f'INSERT INTO users ({", ".join(columns)}) VALUES ({", ".join("?" for _ in columns)})', values)
        conn.commit()
    finally:
        conn.close()
    g.pop("settings", None)
    for name in os.listdir(current_app.config["UPLOAD_DIR"]):
        try:
            os.remove(os.path.join(current_app.config["UPLOAD_DIR"], name))
        except OSError:
            pass
    log_activity("Logiciel réinitialisé à l'état d'origine", f"Copie de sécurité : {safety}")
    flash(f"Le logiciel a été remis à l'état d'origine. Seul votre compte Super-admin est conservé. "
          f"Copie de sécurité : {safety}.", "success")
    return redirect(url_for("main.dashboard"))
