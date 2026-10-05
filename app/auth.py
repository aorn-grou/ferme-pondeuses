"""Connexion, première connexion, mot de passe oublié et profil."""
import random
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import execute, now_utc, query
from .mailer import email_configured, send_email
from .security import SECURITY_QUESTIONS, login_required, normalize_answer, password_problem
from .utils import get_setting, log_activity, parse_utc

bp = Blueprint("auth", __name__)

MAX_ATTEMPTS = 5
LOCK_MINUTES = 15
CODE_MINUTES = 15


# ---------------------------------------------------------------------------
# Verrouillage après plusieurs erreurs
# ---------------------------------------------------------------------------
def _locked_minutes(user):
    until = parse_utc(user["locked_until"])
    if until and until > datetime.now(timezone.utc):
        return int((until - datetime.now(timezone.utc)).total_seconds() // 60) + 1
    return 0


def _register_failure(user):
    attempts = user["failed_attempts"] + 1
    locked = ""
    if attempts >= MAX_ATTEMPTS:
        locked = (datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        attempts = 0
        log_activity("Compte verrouillé", f"{MAX_ATTEMPTS} essais incorrects", user=user)
    execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (attempts, locked, user["id"]))
    return MAX_ATTEMPTS - attempts if not locked else 0


def _clear_failures(user_id):
    execute("UPDATE users SET failed_attempts = 0, locked_until = '' WHERE id = ?", (user_id,))


def _find_user(username):
    return query(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND deleted = 0",
        ((username or "").strip(),), one=True,
    )


def _start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["last_seen"] = datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Connexion / déconnexion
# ---------------------------------------------------------------------------
@bp.route("/connexion", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = _find_user(username)
        if user is None:
            flash("Nom d'utilisateur ou mot de passe incorrect.", "error")
        elif not user["active"]:
            flash("Ce compte est désactivé. Contactez l'administrateur.", "error")
        elif _locked_minutes(user):
            flash(f"Trop d'essais incorrects. Réessayez dans {_locked_minutes(user)} minute(s) "
                  "ou utilisez « Mot de passe oublié ».", "error")
        elif not check_password_hash(user["password_hash"], password):
            left = _register_failure(user)
            if left:
                flash(f"Nom d'utilisateur ou mot de passe incorrect. Encore {left} essai(s).", "error")
            else:
                flash(f"Compte bloqué pendant {LOCK_MINUTES} minutes après trop d'erreurs.", "error")
        else:
            _clear_failures(user["id"])
            execute("UPDATE users SET last_login = ? WHERE id = ?", (now_utc(), user["id"]))
            _start_session(user)
            log_activity("Connexion", user=user)
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("main.dashboard")
            return redirect(target)
    return render_template("auth/login.html")


@bp.route("/deconnexion", methods=["POST"])
def logout():
    if g.get("user"):
        log_activity("Déconnexion")
    session.clear()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Première connexion
# ---------------------------------------------------------------------------
@bp.route("/premiere-connexion", methods=["GET", "POST"])
@login_required
def first_setup():
    user = g.user
    if not user["must_change_password"] and user["setup_done"]:
        return redirect(url_for("main.dashboard"))
    questions = list(SECURITY_QUESTIONS)
    random.shuffle(questions)
    suggested = questions[0]
    form = {"email": user["email"], "question": user["security_question"] or suggested, "custom_question": ""}

    if request.method == "POST":
        form.update({k: request.form.get(k, "").strip() for k in ("email", "question", "custom_question")})
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        answer = request.form.get("answer", "")
        question = form["custom_question"] if form["question"] == "__autre__" else form["question"]
        errors = []
        if user["must_change_password"]:
            problem = password_problem(password)
            if problem:
                errors.append(problem)
            elif password != confirm:
                errors.append("Les deux mots de passe ne sont pas identiques.")
            elif check_password_hash(user["password_hash"], password):
                errors.append("Choisissez un mot de passe différent de l'ancien.")
        if form["email"] and "@" not in form["email"]:
            errors.append("L'adresse e-mail n'est pas valide.")
        if len(question) < 8:
            errors.append("Choisissez ou écrivez une question secrète.")
        if len(normalize_answer(answer)) < 2:
            errors.append("Donnez une réponse à votre question secrète.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            password_hash = generate_password_hash(password) if user["must_change_password"] else user["password_hash"]
            execute(
                """UPDATE users SET password_hash = ?, must_change_password = 0, setup_done = 1,
                       email = ?, security_question = ?, security_answer_hash = ? WHERE id = ?""",
                (password_hash, form["email"], question,
                 generate_password_hash(normalize_answer(answer)), user["id"]),
            )
            log_activity("Première connexion terminée", "Mot de passe et question secrète définis")
            flash("Votre compte est prêt. Bienvenue !", "success")
            return redirect(url_for("main.dashboard"))

    return render_template("auth/first_setup.html", form=form, questions=questions, suggested=suggested,
                           must_change=bool(user["must_change_password"]))


# ---------------------------------------------------------------------------
# Mot de passe oublié
# ---------------------------------------------------------------------------
@bp.route("/mot-de-passe-oublie", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        user = _find_user(request.form.get("username", ""))
        if user is None or not user["active"]:
            flash("Aucun compte actif ne porte ce nom d'utilisateur.", "error")
        else:
            session["reset_user_id"] = user["id"]
            return redirect(url_for("auth.forgot_choice"))
    return render_template("auth/forgot.html")


def _reset_user():
    user_id = session.get("reset_user_id")
    if not user_id:
        return None
    return query("SELECT * FROM users WHERE id = ? AND deleted = 0 AND active = 1", (user_id,), one=True)


def _apply_new_password(user, password, confirm):
    problem = password_problem(password)
    if problem:
        flash(problem, "error")
        return False
    if password != confirm:
        flash("Les deux mots de passe ne sont pas identiques.", "error")
        return False
    execute(
        """UPDATE users SET password_hash = ?, must_change_password = 0, failed_attempts = 0,
               locked_until = '', reset_code_hash = '', reset_code_expires = '' WHERE id = ?""",
        (generate_password_hash(password), user["id"]),
    )
    session.pop("reset_user_id", None)
    flash("Mot de passe modifié. Vous pouvez vous connecter.", "success")
    return True


@bp.route("/mot-de-passe-oublie/choix")
def forgot_choice():
    user = _reset_user()
    if user is None:
        return redirect(url_for("auth.forgot"))
    has_question = bool(user["security_question"] and user["security_answer_hash"])
    can_email = email_configured() and bool(user["email"])
    return render_template("auth/forgot_choice.html", user=user, has_question=has_question, can_email=can_email)


@bp.route("/mot-de-passe-oublie/question", methods=["GET", "POST"])
def forgot_question():
    user = _reset_user()
    if user is None or not user["security_answer_hash"]:
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        if _locked_minutes(user):
            flash(f"Trop d'essais incorrects. Réessayez dans {_locked_minutes(user)} minute(s).", "error")
        elif not check_password_hash(user["security_answer_hash"], normalize_answer(request.form.get("answer"))):
            left = _register_failure(user)
            flash(f"Réponse incorrecte. Encore {left} essai(s)." if left else
                  f"Trop d'erreurs : réessayez dans {LOCK_MINUTES} minutes.", "error")
        elif _apply_new_password(user, request.form.get("password", ""), request.form.get("confirm", "")):
            log_activity("Mot de passe réinitialisé", "Par la question secrète", user=user)
            return redirect(url_for("auth.login"))
    return render_template("auth/forgot_question.html", user=user)


def _mask_email(email):
    name, _, domain = email.partition("@")
    return (name[:2] + "•••" + "@" + domain) if domain else email


@bp.route("/mot-de-passe-oublie/email", methods=["GET", "POST"])
def forgot_email():
    user = _reset_user()
    if user is None or not user["email"] or not email_configured():
        return redirect(url_for("auth.forgot"))

    if request.method == "POST" and request.form.get("action") == "send":
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = (datetime.now(timezone.utc) + timedelta(minutes=CODE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        ok, error = send_email(
            user["email"],
            f"{get_setting('company_name')} : code de réinitialisation",
            f"Bonjour {user['full_name'] or user['username']},\n\n"
            f"Votre code pour changer votre mot de passe est : {code}\n"
            f"Il est valable {CODE_MINUTES} minutes.\n\n"
            "Si vous n'avez rien demandé, ignorez ce message.",
        )
        if ok:
            execute("UPDATE users SET reset_code_hash = ?, reset_code_expires = ? WHERE id = ?",
                    (generate_password_hash(code), expires, user["id"]))
            flash(f"Code envoyé à {_mask_email(user['email'])}. Regardez aussi dans les spams.", "success")
            session["reset_code_sent"] = True
        else:
            flash(error, "error")
        return redirect(url_for("auth.forgot_email"))

    if request.method == "POST":
        expires = parse_utc(user["reset_code_expires"])
        code = request.form.get("code", "").strip()
        if _locked_minutes(user):
            flash(f"Trop d'essais incorrects. Réessayez dans {_locked_minutes(user)} minute(s).", "error")
        elif not user["reset_code_hash"] or not expires or expires < datetime.now(timezone.utc):
            flash("Ce code a expiré. Demandez un nouveau code.", "error")
        elif not check_password_hash(user["reset_code_hash"], code):
            left = _register_failure(user)
            flash(f"Code incorrect. Encore {left} essai(s)." if left else
                  f"Trop d'erreurs : réessayez dans {LOCK_MINUTES} minutes.", "error")
        elif _apply_new_password(user, request.form.get("password", ""), request.form.get("confirm", "")):
            session.pop("reset_code_sent", None)
            log_activity("Mot de passe réinitialisé", "Par code e-mail", user=user)
            return redirect(url_for("auth.login"))

    return render_template("auth/forgot_email.html", user=user, masked=_mask_email(user["email"]),
                           code_sent=session.get("reset_code_sent", False))


# ---------------------------------------------------------------------------
# Mon profil
# ---------------------------------------------------------------------------
@bp.route("/mon-profil", methods=["GET", "POST"])
@login_required
def profile():
    user = g.user
    if request.method == "POST":
        action = request.form.get("action")
        if action == "infos":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()
            if email and "@" not in email:
                flash("L'adresse e-mail n'est pas valide.", "error")
            else:
                execute("UPDATE users SET full_name = ?, email = ?, phone = ? WHERE id = ?",
                        (full_name, email, phone, user["id"]))
                log_activity("Profil modifié")
                flash("Informations enregistrées.", "success")
        elif action == "password":
            current = request.form.get("current", "")
            password = request.form.get("password", "")
            if not check_password_hash(user["password_hash"], current):
                flash("Le mot de passe actuel est incorrect.", "error")
            elif password_problem(password):
                flash(password_problem(password), "error")
            elif password != request.form.get("confirm", ""):
                flash("Les deux nouveaux mots de passe ne sont pas identiques.", "error")
            else:
                execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
                log_activity("Mot de passe modifié")
                flash("Mot de passe modifié.", "success")
        elif action == "question":
            question = request.form.get("custom_question", "").strip() if request.form.get("question") == "__autre__" \
                else request.form.get("question", "").strip()
            answer = request.form.get("answer", "")
            if len(question) < 8 or len(normalize_answer(answer)) < 2:
                flash("Choisissez une question et donnez une réponse.", "error")
            elif not check_password_hash(user["password_hash"], request.form.get("current_q", "")):
                flash("Le mot de passe actuel est incorrect.", "error")
            else:
                execute("UPDATE users SET security_question = ?, security_answer_hash = ? WHERE id = ?",
                        (question, generate_password_hash(normalize_answer(answer)), user["id"]))
                log_activity("Question secrète modifiée")
                flash("Question secrète enregistrée.", "success")
        return redirect(url_for("auth.profile"))
    return render_template("auth/profile.html", user=user, questions=SECURITY_QUESTIONS)
