"""Rôles, droits d'accès, protection des formulaires et contrôle de connexion."""
import json
import secrets
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for

# Niveaux d'accès à un module
NONE, VIEW, EDIT, MANAGE = 0, 1, 2, 3
LEVEL_LABELS = {NONE: "Aucun accès", VIEW: "Voir", EDIT: "Voir et saisir", MANAGE: "Tout (y compris supprimer)"}

# Modules du logiciel : clé, libellé, icône, groupe de menu, description courte.
# `built` passe à True à mesure que les modules sont développés.
MODULES = [
    ("dashboard", "Tableau de bord", "home", "Pilotage", "Résumé, courbes et alertes de votre activité.", True),
    ("discussion", "Discussion", "chat", "Pilotage", "Messages entre associés et commentaires sur les saisies.", True),
    ("controles", "Écarts & contrôles", "alert", "Pilotage", "Inventaires, pertes, poules disparues, mortalité anormale : tout ce qui ne colle pas.", True),
    ("matieres", "Achats & matières premières", "box", "Production", "Fournisseurs, achats de matières, stock des matières.", True),
    ("provenderie", "Provenderie", "factory", "Production", "Formules de provende, programmes par semaine, fabrication, stock de provende.", True),
    ("lots", "Lots de poules", "layers", "Élevage", "Entrée des poussins, âge, effectif, morts, provende consommée et à venir.", True),
    ("alimentation", "Alimentation", "wheat", "Élevage", "Distribution quotidienne de provende, consommation par poule et par lot.", True),
    ("sanitaire", "Suivi sanitaire", "shield", "Élevage", "Vaccinations avec alertes, soins, mortalité.", False),
    ("oeufs", "Œufs", "egg", "Élevage", "Ramassage par lot et par jour, taux de ponte, hausses et baisses.", True),
    ("reformes", "Poules de réforme", "tag", "Ventes", "Poules non rentables à vendre.", False),
    ("ventes", "Ventes & clients", "cart", "Ventes", "Ventes d'œufs et de poules, clients, crédits, factures.", False),
    ("depenses", "Dépenses", "receipt", "Finances", "Dépenses par catégorie, rattachées aux lots.", False),
    ("caisse", "Caisse & propriétaires", "wallet", "Finances", "Caisses, apports et retraits des propriétaires, clôture.", False),
    ("personnel", "Personnel & salaires", "users", "Finances", "Employés, présences, avances et salaires.", False),
    ("immobilisations", "Immobilisations", "building", "Finances", "Bâtiments et matériel, amortissement.", False),
    ("rapports", "Rapports", "chart", "Pilotage", "Rapports détaillés, export PDF et Excel.", False),
    ("imports", "Import Excel", "upload", "Administration", "Modèles Excel à compléter et import des données.", False),
    ("utilisateurs", "Utilisateurs", "user-cog", "Administration", "Comptes, rôles et droits d'accès.", True),
    ("parametres", "Paramètres", "settings", "Administration", "Nom, logo, couleurs de la société, e-mail.", True),
    ("sauvegardes", "Sauvegardes", "database", "Administration", "Sauvegarder, restaurer, réinitialiser.", True),
    ("journal", "Journal d'activité", "list", "Administration", "Qui a fait quoi et quand.", True),
]
MODULE_KEYS = [m[0] for m in MODULES]
# Page d'accueil de chaque module développé (les autres affichent « bientôt »)
MODULE_ENDPOINTS = {
    "dashboard": "main.dashboard",
    "matieres": "matieres.index",
    "provenderie": "provenderie.index",
    "utilisateurs": "users.index",
    "parametres": "settings.index",
    "sauvegardes": "backups.index",
    "journal": "main.journal",
    "discussion": "discussion.index",
    "lots": "lots.index",
    "controles": "controles.index",
    "alimentation": "lots.feeding_day",
    "oeufs": "oeufs.index",
}
MODULE_INFO = {m[0]: {"key": m[0], "label": m[1], "icon": m[2], "group": m[3], "desc": m[4], "built": m[5]} for m in MODULES}
MENU_GROUPS = ["Pilotage", "Production", "Élevage", "Ventes", "Finances", "Administration"]

# Modules réservés à l'administration (non attribuables aux autres rôles)
ADMIN_ONLY = {"utilisateurs", "parametres", "sauvegardes", "journal"}
ASSIGNABLE = [k for k in MODULE_KEYS if k not in ADMIN_ONLY]

ROLES = {
    "super_admin": "Super-admin",
    "admin": "Administrateur (propriétaire)",
    "achats": "Responsable achats / matières premières",
    "provenderie": "Responsable provenderie",
    "elevage": "Responsable élevage (soigneur)",
    "ventes": "Responsable ventes",
    "caissier": "Caissier / comptable",
    "lecteur": "Lecteur (consultation seule)",
}
ADMIN_ROLES = {"super_admin", "admin"}

# Droits par défaut de chaque rôle (modifiables utilisateur par utilisateur)
ROLE_DEFAULTS = {
    "achats": {"dashboard": VIEW, "matieres": MANAGE, "provenderie": VIEW, "depenses": EDIT},
    "provenderie": {"dashboard": VIEW, "matieres": VIEW, "provenderie": MANAGE},
    "elevage": {"dashboard": VIEW, "controles": VIEW, "lots": EDIT, "alimentation": MANAGE, "sanitaire": MANAGE,
                "oeufs": MANAGE, "reformes": EDIT, "provenderie": VIEW},
    "ventes": {"dashboard": VIEW, "oeufs": VIEW, "reformes": EDIT, "ventes": MANAGE},
    "caissier": {"dashboard": VIEW, "depenses": MANAGE, "caisse": MANAGE, "personnel": EDIT, "rapports": VIEW},
    "lecteur": {k: VIEW for k in ASSIGNABLE},
}


def role_label(role):
    return ROLES.get(role, role)


def user_levels(user):
    """Dictionnaire module -> niveau pour un utilisateur."""
    if user is None:
        return {}
    if user["role"] in ADMIN_ROLES:
        return {k: MANAGE for k in MODULE_KEYS}
    levels = dict(ROLE_DEFAULTS.get(user["role"], {}))
    if user["perms"]:
        try:
            custom = json.loads(user["perms"])
            levels = {k: int(v) for k, v in custom.items() if k in ASSIGNABLE}
        except (ValueError, TypeError):
            pass
    levels.setdefault("dashboard", VIEW)  # tout le monde a son tableau de bord
    return levels


def can(module, level=VIEW, user=None):
    user = user if user is not None else g.get("user")
    return user_levels(user).get(module, NONE) >= level


def can_correct(module, row):
    """Corriger une saisie : les responsables (droit « Tout ») corrigent tout ;
    un employé (droit « Voir et saisir ») corrige seulement sa propre saisie du jour."""
    if row is None:
        return False
    if can(module, MANAGE):
        return True
    if not can(module, EDIT):
        return False
    if "created_by" not in row.keys() or row["created_by"] != g.user["id"]:
        return False
    from .stock import today
    from .utils import local_datetime

    return local_datetime(row["created_at"], "%Y-%m-%d") == today()


def is_admin(user=None):
    user = user if user is not None else g.get("user")
    return bool(user) and user["role"] in ADMIN_ROLES


def is_super_admin(user=None):
    user = user if user is not None else g.get("user")
    return bool(user) and user["role"] == "super_admin"


# ---------------------------------------------------------------------------
# Décorateurs
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def require(module, level=VIEW):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not can(module, level):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def super_admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_super_admin():
            flash("Action réservée au Super-admin.", "error")
            return redirect(url_for("main.dashboard"))
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Protection CSRF (empêche un autre site d'envoyer des formulaires à votre place)
# ---------------------------------------------------------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def check_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf", "")
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, description="La page a expiré. Rechargez-la et recommencez.")


# ---------------------------------------------------------------------------
# Mots de passe et réponses secrètes
# ---------------------------------------------------------------------------
SECURITY_QUESTIONS = [
    "Quel est le nom de votre premier animal de compagnie ?",
    "Dans quelle ville est née votre mère ?",
    "Quel est le prénom de votre meilleur ami d'enfance ?",
    "Quel est le nom de votre école primaire ?",
    "Quel était le surnom que l'on vous donnait enfant ?",
    "Quel est le plat préféré de votre grand-mère ?",
    "Dans quel village ou quartier avez-vous grandi ?",
    "Quel est le prénom de votre premier enseignant ?",
    "Quelle est la marque de votre premier téléphone ?",
    "Quel est le deuxième prénom de votre père ?",
]


def normalize_answer(text):
    """Réponse insensible aux majuscules, accents et espaces en trop."""
    import unicodedata

    text = unicodedata.normalize("NFKD", (text or "").strip().lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def password_problem(password):
    """Retourne un message si le mot de passe est trop faible, sinon None."""
    if len(password or "") < 8:
        return "Le mot de passe doit contenir au moins 8 caractères."
    if password.isdigit() or password.isalpha():
        return "Le mot de passe doit mélanger lettres et chiffres."
    return None
