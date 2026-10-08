"""Saisie libre : les listes du logiciel acceptent un nom écrit à la main.

Le navigateur envoie « __new__:Nom|unité » quand le nom n'existe pas encore.
Avant de traiter le formulaire, on retrouve l'élément portant ce nom
(sans tenir compte des majuscules) ou on le crée, puis on remplace la valeur
par son numéro. Le reste du logiciel n'y voit qu'un choix normal dans la liste.
"""
from flask import flash, g, request
from werkzeug.datastructures import MultiDict

from .db import execute, now_utc, query
from .security import EDIT, can
from .utils import log_activity

PREFIX = "__new__:"
BLUEPRINTS = {"matieres", "provenderie", "lots"}
# Fabriquer une provende demande sa composition : pas de création automatique ici
NO_FORMULA_CREATION = {"provenderie.production_new", "provenderie.production_edit"}


def _clean(text, limit=80):
    return " ".join((text or "").split())[:limit]


def _material(name, unit):
    row = query("SELECT id, active FROM materials WHERE name = ? COLLATE NOCASE", (name,), one=True)
    if row:
        if not row["active"]:
            execute("UPDATE materials SET active = 1 WHERE id = ?", (row["id"],))
        return row["id"]
    new_id = execute("INSERT INTO materials (name, unit, created_at) VALUES (?, ?, ?)",
                     (name, _clean(unit, 15) or "kg", now_utc()))
    log_activity("Matière ajoutée (saisie libre)", f"{name} ({unit or 'kg'})")
    flash(f"Nouvelle matière « {name} » créée. Vous pourrez compléter sa fiche (catégorie, seuil d'alerte).", "info")
    return new_id


def _account(name):
    row = query("SELECT id, active FROM accounts WHERE name = ? COLLATE NOCASE", (name,), one=True)
    if row:
        if not row["active"]:
            execute("UPDATE accounts SET active = 1 WHERE id = ?", (row["id"],))
        return row["id"]
    new_id = execute("INSERT INTO accounts (name, kind, sort) VALUES (?, 'autre', 99)", (name,))
    log_activity("Compte d'argent ajouté (saisie libre)", name)
    flash(f"Nouveau compte d'argent « {name} » créé.", "info")
    return new_id


def _formula(name):
    row = query("SELECT id, active FROM formulas WHERE name = ? COLLATE NOCASE", (name,), one=True)
    if row:
        if not row["active"]:
            execute("UPDATE formulas SET active = 1 WHERE id = ?", (row["id"],))
        return row["id"]
    if request.endpoint in NO_FORMULA_CREATION or request.blueprint == "lots":
        flash(f"La formule « {name} » n'existe pas encore. Créez d'abord sa composition "
              "dans Provenderie → Formules, puis fabriquez-la avant de la distribuer.", "error")
        return ""
    new_id = execute("INSERT INTO formulas (name, base_qty, notes, created_at) VALUES (?, 100, ?, ?)",
                     (name, "Créée en saisie libre — composition à compléter.", now_utc()))
    log_activity("Formule ajoutée (saisie libre)", name)
    flash(f"Nouvelle formule « {name} » créée. Pensez à compléter sa composition.", "info")
    return new_id


def _supplier(name):
    row = query("SELECT id FROM suppliers WHERE name = ? COLLATE NOCASE", (name,), one=True)
    if row:
        return row["id"]
    new_id = execute("INSERT INTO suppliers (name, phone, address, notes, created_at) VALUES (?, '', '', '', ?)",
                     (name, now_utc()))
    log_activity("Fournisseur ajouté (saisie libre)", name)
    return new_id


RESOLVERS = {
    "material_id": lambda name, extra: _material(name, extra),
    "account_id": lambda name, extra: _account(name),
    "formula_id": lambda name, extra: _formula(name),
    "supplier_id": lambda name, extra: _supplier(name),
}


def resolve_free_entries():
    """Appelé avant chaque formulaire envoyé (POST) des modules de saisie."""
    if request.method != "POST" or request.blueprint not in BLUEPRINTS or g.get("user") is None:
        return
    if not any(v.startswith(PREFIX) for _, v in request.form.items(multi=True)):
        return
    module = request.blueprint
    if module == "lots" and request.endpoint in ("lots.feeding_day", "lots.feeding_new"):
        module = "alimentation"
    if not can(module, EDIT):
        return
    cache = {}
    items = []
    for key, value in request.form.items(multi=True):
        if key in RESOLVERS and value.startswith(PREFIX):
            raw = value[len(PREFIX):]
            name, _, extra = raw.partition("|")
            name = _clean(name)
            if len(name) < 2:
                value = ""
            else:
                cache_key = (key, name.lower())
                if cache_key not in cache:
                    cache[cache_key] = RESOLVERS[key](name, extra)
                value = str(cache[cache_key])
        items.append((key, value))
    request.form = MultiDict(items)
