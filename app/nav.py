"""Menu par module (comme une application) : quand on entre dans un module, le menu de gauche
montre seulement ses pages (« Sections ») et ses boutons d'action (« Actions »),
avec « ← Menu principal » pour revenir à tous les modules.

Une action peut ouvrir un formulaire de la page au milieu de l'écran (« modal ») :
on donne alors `open` = l'identifiant de la carte du formulaire dans la page.
"""
from flask import request, url_for

from .security import EDIT, VIEW, can

# Section : (libellé, icône, endpoint, arguments, autres endpoints de la même section, (module, niveau) ou None)
# Action : (libellé, icône, endpoint, arguments, carte à ouvrir au milieu ou None, (module, niveau))
S, A = "section", "action"

MODULE_NAV = {
    "matieres": {
        "title": "Achats & matières premières", "icon": "box", "help": "matieres",
        "sections": [
            ("Stock matières premières", "box", "matieres.index", {}, ["matieres.material", "matieres.material_edit",
                                                                    "matieres.movement", "matieres.archived"], None),
            ("Achats", "cart", "matieres.purchases", {}, ["matieres.purchase", "matieres.purchase_edit",
                                                                 "matieres.purchase_new"], None),
            ("Fournisseurs & dettes", "users", "matieres.suppliers", {}, ["matieres.supplier", "matieres.supplier_edit",
                                                                         "matieres.supplier_new"], None),
            ("Stock & historique", "clock", "matieres.history", {}, [], None),
            ("Inventaire", "list", "matieres.inventory", {}, [], ("matieres", EDIT)),
            ("Catégories", "tag", "matieres.categories", {}, [], ("matieres", EDIT)),
            ("Stock provende finie", "wheat", "provenderie.index", {}, [], ("provenderie", VIEW)),
        ],
        "actions": [
            ("Nouvel achat", "cart", "matieres.purchase_new", {}, None, ("matieres", EDIT)),
            ("Nouvelle matière", "plus", "matieres.material_new", {}, None, ("matieres", EDIT)),
            ("Perte / correction", "alert", "matieres.movement", {}, None, ("matieres", EDIT)),
        ],
    },
    "provenderie": {
        "title": "Provenderie", "icon": "factory", "help": "provenderie",
        # dans l'ordre du travail : 1. la recette, 2. le programme, 3. fabriquer, 4. le stock obtenu
        "sections": [
            ("1. Formules (recettes)", "list", "provenderie.formulas", {}, ["provenderie.formula", "provenderie.formula_new",
                                                                           "provenderie.formula_edit"], None),
            ("2. Programmes par semaine", "clock", "provenderie.programs", {}, ["provenderie.program", "provenderie.program_new",
                                                                               "provenderie.program_edit"], None),
            ("3. Fabrications", "factory", "provenderie.productions", {}, ["provenderie.production", "provenderie.production_new"], None),
            ("4. Stock provende finie", "wheat", "provenderie.index", {}, ["provenderie.feed_history", "provenderie.movement"], None),
            ("Historique & durée", "clock", "provenderie.feed_history_all", {}, [], None),
            ("Stock matières premières", "box", "matieres.index", {}, [], ("matieres", VIEW)),
        ],
        "actions": [
            ("Fabriquer", "factory", "provenderie.production_new", {}, None, ("provenderie", EDIT)),
            ("Nouvelle formule", "plus", "provenderie.formula_new", {}, None, ("provenderie", EDIT)),
            ("Perte / inventaire", "alert", "provenderie.movement", {}, None, ("provenderie", EDIT)),
        ],
    },
    "lots": {
        "title": "Lots de poules", "icon": "layers", "help": "lots",
        "sections": [
            ("Lots de poules", "layers", "lots.index", {}, ["lots.lot", "lots.lot_edit", "lots.lot_new"], None),
            ("Provende du jour", "wheat", "lots.feeding_day", {}, [], ("alimentation", VIEW)),
            ("Ramassage des œufs", "egg", "oeufs.index", {}, [], ("oeufs", VIEW)),
            ("Suivi sanitaire", "shield", "sanitaire.index", {}, [], ("sanitaire", VIEW)),
            ("Stock provende finie", "box", "provenderie.index", {}, [], ("provenderie", VIEW)),
        ],
        "actions": [
            ("Nouveau lot", "plus", "lots.lot_new", {}, None, ("lots", EDIT)),
            ("Donner la provende", "wheat", "lots.feeding_day", {}, None, ("alimentation", EDIT)),
        ],
    },
    "oeufs": {
        "title": "Œufs", "icon": "egg", "help": "oeufs",
        "sections": [
            ("Ramassage du jour", "egg", "oeufs.index", {}, [], None),
            ("Prix des œufs", "chart", "prix.index", {}, [], ("ventes", VIEW)),
            ("Stock d'œufs", "box", "ventes.stock", {}, [], ("ventes", VIEW)),
            ("Lots de poules", "layers", "lots.index", {}, [], ("lots", VIEW)),
        ],
        "actions": [
            ("Nouvelle vente", "cart", "ventes.sale_new", {}, None, ("ventes", EDIT)),
            ("Changer le prix du jour", "edit", "prix.index", {}, "prix-du-jour", ("ventes", EDIT)),
        ],
    },
    "sanitaire": {
        "title": "Suivi sanitaire", "icon": "shield", "help": "sanitaire",
        "sections": [
            ("Soins & alertes", "shield", "sanitaire.index", {}, [], None),
            ("Programmes", "clock", "sanitaire.programs", {}, ["sanitaire.program", "sanitaire.program_new", "sanitaire.program_edit"], None),
            ("Lots de poules", "layers", "lots.index", {}, [], ("lots", VIEW)),
        ],
        "actions": [
            ("Ajouter un soin", "plus", "sanitaire.index", {}, "nouveau", ("sanitaire", EDIT)),
            ("Nouveau programme", "list", "sanitaire.program_new", {}, None, ("sanitaire", EDIT)),
        ],
    },
    "ventes": {
        "title": "Ventes & clients", "icon": "cart", "help": "ventes",
        "sections": [
            ("Ventes", "cart", "ventes.index", {}, ["ventes.sale", "ventes.sale_new"], None),
            ("Prix des œufs", "chart", "prix.index", {}, [], None),
            ("Stock d'œufs", "egg", "ventes.stock", {}, [], None),
            ("Clients & crédits", "users", "ventes.clients", {}, ["ventes.client"], None),
            ("Fumier", "sprout", "fumier.index", {}, [], ("fumier", VIEW)),
            ("Poules de réforme", "tag", "reformes.index", {}, [], ("reformes", VIEW)),
        ],
        "actions": [
            ("Nouvelle vente", "plus", "ventes.sale_new", {}, None, ("ventes", EDIT)),
            ("Changer le prix du jour", "edit", "prix.index", {}, "prix-du-jour", ("ventes", EDIT)),
        ],
    },
    "fumier": {
        "title": "Fumier", "icon": "sprout", "help": "fumier",
        "sections": [
            ("Fumier : tas & ventes", "sprout", "fumier.index", {}, [], None),
            ("Lots de poules", "layers", "lots.index", {}, [], ("lots", VIEW)),
            ("Ventes", "cart", "ventes.index", {}, [], ("ventes", VIEW)),
        ],
        "actions": [
            ("Enlever du fumier", "plus", "fumier.index", {}, "enlever", ("fumier", EDIT)),
            ("Vendre du fumier", "cart", "fumier.index", {}, "vendre", ("fumier", EDIT)),
        ],
    },
    "reformes": {
        "title": "Poules de réforme", "icon": "tag", "help": "reformes",
        "sections": [
            ("Conseil & ventes", "tag", "reformes.index", {}, [], None),
            ("Lots de poules", "layers", "lots.index", {}, [], ("lots", VIEW)),
        ],
        "actions": [("Vendre des poules", "cart", "reformes.index", {}, "vendre", ("reformes", EDIT))],
    },
    "depenses": {
        "title": "Dépenses", "icon": "receipt", "help": "depenses",
        "sections": [
            ("Toutes les dépenses", "receipt", "depenses.index", {}, ["depenses.item", "depenses.edit"], None),
            ("À payer", "alert", "depenses.index", {"voir": "a_payer"}, [], None),
        ],
        "actions": [("Nouvelle dépense", "plus", "depenses.index", {}, "nouvelle", ("depenses", EDIT))],
    },
    "caisse": {
        "title": "Caisse & propriétaires", "icon": "wallet", "help": "caisse",
        "sections": [
            ("Caisses & propriétaires", "wallet", "caisse.index", {}, ["caisse.account"], None),
        ],
        "actions": [
            ("Apport", "plus", "caisse.index", {"op": "apport"}, "operations", ("caisse", EDIT)),
            ("Retrait", "logout", "caisse.index", {"op": "retrait"}, "operations", ("caisse", EDIT)),
            ("Transfert", "refresh", "caisse.index", {"op": "transfert"}, "operations", ("caisse", EDIT)),
            ("Comptage de caisse", "check", "caisse.index", {"op": "comptage"}, "operations", ("caisse", EDIT)),
        ],
    },
    "personnel": {
        "title": "Personnel & salaires", "icon": "users", "help": "personnel",
        "sections": [("Fiche de paie du mois", "users", "personnel.index", {}, ["personnel.employee", "personnel.employee_edit"], None)],
        "actions": [("Nouvel employé", "plus", "personnel.employee_new", {}, None, ("personnel", EDIT))],
    },
    "immobilisations": {
        "title": "Immobilisations", "icon": "building", "help": "immobilisations",
        "sections": [("Bâtiments & matériel", "building", "immobilisations.index", {}, ["immobilisations.asset"], None)],
        "actions": [("Nouveau bien", "plus", "immobilisations.index", {}, "nouveau", ("immobilisations", EDIT))],
    },
    "rapports": {
        "title": "Rapports", "icon": "chart", "help": "rapports",
        "sections": [
            ("💰 L'argent", "wallet", "rapports.index", {"_anchor": "argent"}, [], None),
            ("🐔 Rentabilité des lots", "layers", "rapports.index", {"_anchor": "lots"}, [], None),
            ("📈 Pics, creux et moyennes", "chart", "rapports.index", {"_anchor": "tendances"}, [], None),
            ("Prix des œufs", "egg", "prix.index", {}, [], ("ventes", VIEW)),
        ],
        "actions": [],
    },
}
MODULE_NAV["alimentation"] = dict(MODULE_NAV["lots"], title="Alimentation", icon="wheat", help="alimentation")

# Blueprint → module (pour savoir dans quel module on est)
BP_MODULE = {
    "matieres": "matieres", "provenderie": "provenderie", "lots": "lots", "sanitaire": "sanitaire", "oeufs": "oeufs",
    "ventes": "ventes", "prix": "ventes", "depenses": "depenses", "caisse": "caisse", "personnel": "personnel",
    "immobilisations": "immobilisations", "reformes": "reformes", "rapports": "rapports", "fumier": "fumier",
}
ENDPOINT_MODULE = {"lots.feeding_day": "alimentation"}

# Pages qui contiennent surtout un formulaire : on les centre au milieu de l'écran
FORM_ENDPOINTS = {
    "matieres.purchase_new", "matieres.purchase_edit", "matieres.material_new", "matieres.material_edit",
    "matieres.supplier_new", "matieres.supplier_edit", "matieres.movement", "provenderie.formula_new",
    "provenderie.formula_edit", "provenderie.program_new", "provenderie.program_edit", "provenderie.production_new",
    "provenderie.movement", "lots.lot_new", "lots.lot_edit", "ventes.sale_new", "sanitaire.program_new",
    "sanitaire.program_edit", "personnel.employee_new", "personnel.employee_edit", "depenses.edit", "users.create",
    "users.edit", "auth.profile",
}


def current_module():
    ep = request.endpoint or ""
    return ENDPOINT_MODULE.get(ep) or BP_MODULE.get(request.blueprint or "")


def _url(endpoint, args):
    args = dict(args)
    anchor = args.pop("_anchor", None)
    return url_for(endpoint, **args) + (f"#{anchor}" if anchor else "")


def module_nav():
    """Le menu du module ouvert, prêt à afficher (ou None sur les pages sans menu de module)."""
    key = current_module()
    spec = MODULE_NAV.get(key)
    if not spec or not can(key if key != "alimentation" else "alimentation"):
        return None
    ep = request.endpoint
    args = request.args
    sections = []
    for label, ic, endpoint, eargs, also, perm in spec["sections"]:
        if perm and not can(*perm):
            continue
        clean = {k: v for k, v in eargs.items() if not k.startswith("_")}
        same = ep == endpoint and not eargs.get("_anchor")
        if clean:
            active = same and all(args.get(k) == v for k, v in clean.items())
        else:
            active = same and not args.get("voir")
        sections.append({"label": label, "icon": ic, "url": _url(endpoint, eargs), "active": active or ep in also,
                         "endpoint": endpoint})
    if sections and not any(s["active"] for s in sections):
        for s in sections:
            if s["endpoint"] == ep:
                s["active"] = True
                break
    actions = []
    for label, ic, endpoint, eargs, open_id, perm in spec["actions"]:
        if perm and not can(*perm):
            continue
        params = dict(eargs)
        if open_id:
            params["ouvrir"] = open_id
        actions.append({"label": label, "icon": ic, "url": url_for(endpoint, **params), "open": open_id,
                        "op": eargs.get("op"), "same_page": ep == endpoint})
    return {"key": key, "title": spec["title"], "icon": spec["icon"], "help": spec["help"], "sections": sections,
            "actions": actions}


def all_pages():
    """Toutes les pages et actions accessibles (pour la recherche)."""
    from .security import MODULE_ENDPOINTS, MODULE_INFO, MODULES

    out, seen = [], set()
    for key, label, ic, group, desc, built in MODULES:
        if not can(key) or key not in MODULE_ENDPOINTS:
            continue
        out.append({"label": label, "detail": desc, "icon": ic, "url": url_for(MODULE_ENDPOINTS[key]), "kind": "Page"})
        seen.add(label)
    for key, spec in MODULE_NAV.items():
        if not can(key):
            continue
        for label, ic, endpoint, eargs, also, perm in spec["sections"]:
            if (perm and not can(*perm)) or label in seen:
                continue
            seen.add(label)
            out.append({"label": label.split(". ", 1)[-1], "detail": spec["title"], "icon": ic, "url": _url(endpoint, eargs),
                        "kind": "Page"})
        for label, ic, endpoint, eargs, open_id, perm in spec["actions"]:
            if (perm and not can(*perm)) or label in seen:
                continue
            seen.add(label)
            params = dict(eargs)
            if open_id:
                params["ouvrir"] = open_id
            out.append({"label": label, "detail": spec["title"], "icon": ic, "url": url_for(endpoint, **params), "kind": "Action"})
    return out


__all__ = ["module_nav", "current_module", "all_pages", "FORM_ENDPOINTS", "MODULE_NAV"]
