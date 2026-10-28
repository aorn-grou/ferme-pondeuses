"""Recherche dans tout le logiciel (barre en haut de chaque page).

On cherche sans tenir compte des accents ni des majuscules (« oeuf » trouve « Œufs »),
dans les pages et actions, les lots, clients, fournisseurs, matières, formules, employés,
biens, dépenses, ventes, achats, soins, comptes et utilisateurs — seulement ce que
la personne a le droit de voir. Chaque résultat ouvre la page où on peut le modifier.
"""
import unicodedata

from flask import Blueprint, jsonify, render_template, request, url_for

from .db import query
from .security import VIEW, can, is_admin, login_required
from .utils import date_fr, fmt_money

bp = Blueprint("recherche", __name__, url_prefix="/recherche")


def norm(text):
    text = str(text or "").replace("œ", "oe").replace("Œ", "Oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def _match(q_words, *fields):
    hay = " ".join(norm(f) for f in fields if f)
    return all(w in hay for w in q_words)


def _score(q, label):
    n = norm(label)
    if n == q:
        return 0
    if n.startswith(q):
        return 1
    if any(part.startswith(q) for part in n.split()):
        return 2
    return 3


def search(q, limit=None):
    q = norm(q)
    if len(q) < 2 and not q.isdigit():
        return []
    words = q.replace("n°", " ").replace("no ", " ").split()
    num = next((w.lstrip("#") for w in words if w.lstrip("#").isdigit()), None)
    res = []

    def add(group, label, detail, url, icon, rank=None):
        res.append({"group": group, "label": label, "detail": detail, "url": url, "icon": icon,
                    "rank": _score(q, label) if rank is None else rank})

    from .nav import all_pages

    for p in all_pages():
        if _match(words, p["label"], p["detail"]):
            add("Pages et actions", p["label"], p["detail"], p["url"], p["icon"], rank=_score(q, p["label"]) - 0.5)
    if can("lots"):
        for r in query("SELECT * FROM lots ORDER BY status, name"):
            if _match(words, r["name"], r["building"], r["breed"], r["supplier"], r["notes"]):
                add("Lots de poules", r["name"], f"{r['initial_count']} poules au départ · arrivé le {date_fr(r['arrival_date'])}"
                    + (" · terminé" if r["status"] != "actif" else ""), url_for("lots.lot", lot_id=r["id"]), "layers")
    if can("ventes"):
        for r in query("SELECT * FROM clients ORDER BY name"):
            if _match(words, r["name"], r["phone"], r["notes"]):
                add("Clients", r["name"], r["phone"] or "Client", url_for("ventes.client", client_id=r["id"]), "users")
        sales = query("""SELECT s.*, c.name AS client FROM sales s LEFT JOIN clients c ON c.id = s.client_id
                         ORDER BY s.date DESC, s.id DESC LIMIT 3000""")
        for r in sales:
            if (num and str(r["id"]) == num) or (not num and _match(words, r["product"], r["client"], r["notes"])):
                add("Ventes", f"Vente n°{r['id']} · {r['product']}", f"{date_fr(r['date'])} · {r['client'] or 'client de passage'} · "
                    f"{fmt_money(r['total'])}", url_for("ventes.sale", sale_id=r["id"]), "cart", rank=0 if num else None)
    if can("matieres"):
        for r in query("SELECT * FROM materials ORDER BY active DESC, name"):
            if _match(words, r["name"], r["notes"]):
                add("Matières premières", r["name"], f"Compté en {r['unit']}" + ("" if r["active"] else " · archivée"),
                    url_for("matieres.material", material_id=r["id"]), "box")
        for r in query("SELECT * FROM suppliers ORDER BY name"):
            if _match(words, r["name"], r["phone"], r["address"], r["notes"]):
                add("Fournisseurs", r["name"], r["phone"] or "Fournisseur", url_for("matieres.supplier", supplier_id=r["id"]), "users")
        for r in query("""SELECT p.*, s.name AS supplier FROM purchases p LEFT JOIN suppliers s ON s.id = p.supplier_id
                          ORDER BY p.date DESC, p.id DESC LIMIT 2000"""):
            if (num and str(r["id"]) == num) or (not num and _match(words, r["supplier"], r["reference"], r["notes"]) and len(q) >= 3):
                add("Achats", f"Achat n°{r['id']}" + (f" · {r['supplier']}" if r["supplier"] else ""),
                    f"{date_fr(r['date'])} · {fmt_money(r['total'])}", url_for("matieres.purchase", purchase_id=r["id"]), "cart",
                    rank=0 if num else None)
    if can("provenderie"):
        for r in query("SELECT * FROM formulas ORDER BY active DESC, name"):
            if _match(words, r["name"], r["phase"], r["notes"]):
                add("Formules de provende", r["name"], (r["phase"] or "Formule") + ("" if r["active"] else " · archivée"),
                    url_for("provenderie.formula", formula_id=r["id"]), "factory")
        for r in query("SELECT * FROM feed_programs ORDER BY name"):
            if _match(words, r["name"], r["notes"]):
                add("Programmes de provende", r["name"], "Programme par semaine", url_for("provenderie.program", program_id=r["id"]), "clock")
    if can("depenses"):
        for r in query("SELECT * FROM expenses ORDER BY date DESC, id DESC LIMIT 3000"):
            if (num and str(r["id"]) == num) or (not num and _match(words, r["category"], r["label"], r["beneficiary"], r["notes"])):
                add("Dépenses", f"{r['category']}" + (f" · {r['label']}" if r["label"] else ""),
                    f"{date_fr(r['date'])} · {fmt_money(r['amount'])}" + (" · à payer" if r["status"] == "a_payer" else ""),
                    url_for("depenses.item", exp_id=r["id"]), "receipt", rank=0 if num else None)
    if can("personnel"):
        for r in query("SELECT * FROM employees ORDER BY active DESC, name"):
            if _match(words, r["name"], r["job"], r["phone"], r["notes"]):
                add("Employés", r["name"], r["job"] or "Employé", url_for("personnel.employee", employee_id=r["id"]), "users")
    if can("immobilisations"):
        for r in query("SELECT * FROM assets ORDER BY name"):
            if _match(words, r["name"], r["category"], r["notes"]):
                add("Immobilisations", r["name"], f"{r['category'] or 'Bien'} · {fmt_money(r['cost'])}",
                    url_for("immobilisations.asset", asset_id=r["id"]), "building")
    if can("sanitaire"):
        for r in query("""SELECT e.*, l.name AS lot FROM health_events e LEFT JOIN lots l ON l.id = e.lot_id
                          ORDER BY e.due_date DESC LIMIT 2000"""):
            if _match(words, r["title"], r["product"], r["lot"], r["notes"]):
                state = {"fait": "fait", "annule": "annulé"}.get(r["status"], "à faire")
                add("Soins", r["title"], f"{r['lot'] or 'Tous les lots'} · {date_fr(r['done_date'] or r['due_date'])} · {state}",
                    url_for("sanitaire.index") + f"#soin-{r['id']}", "shield")
        for r in query("SELECT * FROM health_programs ORDER BY name"):
            if _match(words, r["name"], r["notes"]):
                add("Programmes sanitaires", r["name"], "Programme de vaccins et soins", url_for("sanitaire.program", program_id=r["id"]), "shield")
    if can("caisse"):
        for r in query("SELECT * FROM accounts WHERE active = 1 ORDER BY sort"):
            if _match(words, r["name"]):
                add("Caisses & propriétaires", r["name"], "Compte d'argent", url_for("caisse.account", account_id=r["id"]), "wallet")
    if is_admin():
        for r in query("SELECT * FROM users WHERE deleted = 0 ORDER BY username"):
            if _match(words, r["username"], r["full_name"], r["email"], r["phone"]):
                add("Utilisateurs", r["full_name"] or r["username"], r["username"], url_for("users.edit", user_id=r["id"]), "user-cog")
    res.sort(key=lambda x: x["rank"])
    # on regroupe par sorte (lots, clients…), la sorte la plus pertinente d'abord
    groups = {}
    for r in res:
        groups.setdefault(r["group"], []).append(r)
    res = [r for items in groups.values() for r in (items[:4] if limit else items)] if limit else \
        [r for items in groups.values() for r in items]
    return res[:limit] if limit else res


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()[:80]
    results = search(q) if q else []
    groups = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    return render_template("recherche/index.html", q=q, groups=groups, total=len(results))


@bp.route("/rapide")
@login_required
def quick():
    q = request.args.get("q", "").strip()[:80]
    return jsonify([{k: r[k] for k in ("group", "label", "detail", "url")} for r in search(q, limit=12)])


__all__ = ["bp", "search", "norm"]
