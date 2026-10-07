"""Outils communs : paramètres, journal d'activité, dates, montants, couleurs."""
from datetime import datetime, timezone

from flask import g, request

from .db import execute, now_utc, query

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Indian/Antananarivo")
except Exception:  # fuseau indisponible : on reste en UTC+3 fixe
    from datetime import timedelta

    LOCAL_TZ = timezone(timedelta(hours=3))

DEFAULT_SETTINGS = {
    "company_name": "Androfia Farm",
    "company_slogan": "Ferme de poules pondeuses",
    "company_address": "",
    "company_phone": "",
    "company_email": "",
    "company_nif": "",
    "company_stat": "",
    "logo_file": "",
    "primary_color": "#1d6b35",
    "menu_color": "#0e3b20",
    "default_theme": "auto",
    "currency": "Ar",
    "session_timeout": "30",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "last_auto_backup": "",
}


# ---------------------------------------------------------------------------
# Paramètres de la société
# ---------------------------------------------------------------------------
def all_settings():
    if "settings" not in g:
        values = dict(DEFAULT_SETTINGS)
        for row in query("SELECT key, value FROM settings"):
            values[row["key"]] = row["value"]
        g.settings = values
    return g.settings


def get_setting(key, default=None):
    return all_settings().get(key, default if default is not None else DEFAULT_SETTINGS.get(key, ""))


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "" if value is None else str(value)),
    )
    g.pop("settings", None)


# ---------------------------------------------------------------------------
# Journal d'activité
# ---------------------------------------------------------------------------
def log_activity(action, details="", user=None):
    user = user if user is not None else g.get("user")
    try:
        ip = request.headers.get("X-Real-IP") or request.remote_addr or ""
    except RuntimeError:
        ip = ""
    execute(
        "INSERT INTO activity_log (user_id, username, action, details, ip, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"] if user else None, user["username"] if user else "", action, details, ip, now_utc()),
    )


# ---------------------------------------------------------------------------
# Dates (stockées en UTC, affichées à l'heure de Madagascar)
# ---------------------------------------------------------------------------
def parse_utc(text):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def local_datetime(text, fmt="%d/%m/%Y %H:%M"):
    dt = parse_utc(text)
    return dt.astimezone(LOCAL_TZ).strftime(fmt) if dt else ""


def local_now():
    return datetime.now(LOCAL_TZ)


# ---------------------------------------------------------------------------
# Montants et nombres
# ---------------------------------------------------------------------------
def fmt_number(value, decimals=0):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:,.{decimals}f}".replace(",", " ").replace(".", ",")
    return text


def fmt_qty(value, unit=""):
    """Quantité : jusqu'à 2 décimales, sans zéros inutiles (12,5 kg ; 3 sacs)."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    text = fmt_number(number, 2)
    if "," in text:
        text = text.rstrip("0").rstrip(",")
    if text in ("-0", ""):
        text = "0"
    return f"{text} {unit}".strip()


def date_fr(text):
    """« 2026-10-06 » devient « 06/10/2026 »."""
    if not text or len(text) < 10:
        return text or ""
    return f"{text[8:10]}/{text[5:7]}/{text[0:4]}"


def fmt_money(value):
    return f"{fmt_number(value)} {get_setting('currency')}"


# ---------------------------------------------------------------------------
# Couleurs
# ---------------------------------------------------------------------------
def valid_hex(color, fallback):
    color = (color or "").strip()
    if len(color) == 7 and color.startswith("#"):
        try:
            int(color[1:], 16)
            return color.lower()
        except ValueError:
            pass
    return fallback


def readable_text_on(color):
    """Blanc ou presque-noir selon la clarté du fond, pour rester lisible."""
    color = valid_hex(color, "#000000")
    r, g_, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * channel(r) + 0.7152 * channel(g_) + 0.0722 * channel(b)
    return "#14201b" if luminance > 0.45 else "#ffffff"
