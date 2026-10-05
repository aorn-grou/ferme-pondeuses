"""Envoi d'e-mails (codes de réinitialisation, alertes).

Fonctionne avec Gmail : dans Paramètres > E-mail, indiquez votre adresse Gmail
et un « mot de passe d'application » Google (pas votre mot de passe habituel).
"""
import smtplib
import ssl
from email.message import EmailMessage

from .utils import get_setting


def email_configured():
    return bool(get_setting("smtp_host") and get_setting("smtp_user") and get_setting("smtp_password"))


def send_email(to, subject, body):
    """Envoie un e-mail. Retourne (succès, message d'erreur)."""
    if not email_configured():
        return False, "L'envoi d'e-mails n'est pas encore configuré (Paramètres > E-mail)."
    host = get_setting("smtp_host")
    try:
        port = int(get_setting("smtp_port") or 587)
    except ValueError:
        port = 587
    user = get_setting("smtp_user")
    sender = get_setting("smtp_from") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{get_setting('company_name')} <{sender}>"
    msg["To"] = to
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
                server.login(user, get_setting("smtp_password"))
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls(context=context)
                server.login(user, get_setting("smtp_password"))
                server.send_message(msg)
        return True, ""
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"Échec de l'envoi : {exc}"
