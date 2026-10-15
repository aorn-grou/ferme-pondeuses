"""Import Excel : reprendre d'un coup des données écrites dans Excel (anciens cahiers, mois passés…).

1. Télécharger le MODÈLE Excel de ce qu'on veut importer (les colonnes sont déjà là, avec un exemple).
2. Le remplir dans Excel (une ligne = une saisie), l'enregistrer.
3. L'envoyer ici : le logiciel VÉRIFIE chaque ligne et montre ce qui est bon (vert) et ce qui ne va pas (rouge, avec la raison).
4. Confirmer : seules les lignes bonnes sont enregistrées, exactement comme si on les avait tapées une par une.
5. Une erreur ? « Annuler cet import » efface tout ce que cet import a ajouté.
"""
import csv
import io
import json
import os
import secrets
from datetime import date as Date, datetime

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for

from .db import now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, require
from .stock import parse_num, today
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("imports", __name__, url_prefix="/import")

TRAY = 30
MAX_ROWS = 3000

# Pour chaque sorte d'import : titre, explication, colonnes (nom, obligatoire, aide), ligne d'exemple
KINDS = {
    "oeufs": {
        "title": "Ramassage des œufs", "emoji": "🥚", "module": "oeufs",
        "help": "Une ligne par lot et par jour. Si ce jour est déjà saisi pour ce lot, la ligne est ignorée.",
        "cols": [("Date", True, "jj/mm/aaaa"), ("Lot", True, "nom exact du lot"), ("Bons œufs", True, "nombre d'œufs"),
                 ("Cassés", False, "nombre (0 si vide)"), ("Remarque", False, "")],
        "example": ["01/09/2026", "Lot A", 1450, 12, ""],
    },
    "ventes": {
        "title": "Ventes", "emoji": "💰", "module": "ventes",
        "help": "Produit « Œufs » : la quantité est en plateaux ou en œufs (colonne Unité). Payé vide = tout payé, 0 = à crédit.",
        "cols": [("Date", True, "jj/mm/aaaa"), ("Client", False, "nom (créé s'il n'existe pas)"), ("Produit", True, "Œufs, Fumier…"),
                 ("Quantité", True, "nombre"), ("Unité", True, "plateau, œuf, sac…"), ("Prix unitaire", True, "Ar, pour 1 unité"),
                 ("Payé", False, "Ar (vide = tout)"), ("Compte", False, "caisse où va l'argent"), ("Lot", False, "facultatif"),
                 ("Remarque", False, "")],
        "example": ["02/09/2026", "Épicerie Rasoa", "Œufs", 20, "plateau", 15000, 200000, "Caisse de la ferme", "", ""],
    },
    "depenses": {
        "title": "Dépenses", "emoji": "🧾", "module": "depenses",
        "help": "Compte vide = dépense « à payer ». Lot vide = dépense commune (partagée entre les lots).",
        "cols": [("Date", True, "jj/mm/aaaa"), ("Poste", True, "Électricité, Transport…"), ("Détail", False, ""),
                 ("Montant", True, "Ar"), ("Lot", False, "vide = commune"), ("Compte", False, "payé avec (vide = à payer)"),
                 ("Payé à", False, ""), ("Remarque", False, "")],
        "example": ["05/09/2026", "Électricité", "Facture août", 85000, "", "Caisse de la ferme", "JIRAMA", ""],
    },
    "mortalite": {
        "title": "Mortalité", "emoji": "🪦", "module": "lots",
        "help": "Une ligne par lot et par jour où des poules sont mortes.",
        "cols": [("Date", True, "jj/mm/aaaa"), ("Lot", True, "nom exact du lot"), ("Nombre de morts", True, "nombre"),
                 ("Raison", False, "maladie, chaleur…")],
        "example": ["03/09/2026", "Lot A", 2, "Chaleur"],
    },
}


def _tmp_dir():
    d = os.path.join(current_app.config["UPLOAD_DIR"], "imports")
    os.makedirs(d, exist_ok=True)
    return d


def has_xlsx():
    try:
        import openpyxl  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Modèles Excel
# ---------------------------------------------------------------------------
def template_bytes(kind):
    spec = KINDS[kind]
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "À remplir"
    head = PatternFill("solid", fgColor="1D6B35")
    for i, (name, req, _hint) in enumerate(spec["cols"], 1):
        c = ws.cell(row=1, column=i, value=name + (" *" if req else ""))
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[c.column_letter].width = max(14, len(name) + 6)
    for i, v in enumerate(spec["example"], 1):
        cell = ws.cell(row=2, column=i, value=v)
        if i == 1:
            cell.value = datetime.strptime(v, "%d/%m/%Y")
            cell.number_format = "DD/MM/YYYY"
    ws.freeze_panes = "A2"
    lots = [r["name"] for r in query("SELECT name FROM lots ORDER BY status = 'actif' DESC, arrival_date DESC")]
    lists = wb.create_sheet("Listes")
    for i, n in enumerate(lots, 1):
        lists.cell(row=i, column=1, value=n)
    for i, n in enumerate([r["name"] for r in query("SELECT name FROM accounts WHERE active = 1 ORDER BY sort")], 1):
        lists.cell(row=i, column=2, value=n)
    names = [c[0] for c in spec["cols"]]
    if lots and "Lot" in names:
        col = ws.cell(row=1, column=names.index("Lot") + 1).column_letter
        dv = DataValidation(type="list", formula1=f"=Listes!$A$1:$A${len(lots)}", allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{MAX_ROWS}")
    help_ws = wb.create_sheet("Mode d'emploi")
    help_ws.column_dimensions["A"].width = 22
    help_ws.column_dimensions["B"].width = 70
    rows = [("Import", spec["title"]), ("Règle", spec["help"]),
            ("", "Une ligne = une saisie. Ne changez pas la première ligne (les titres). Les colonnes avec * sont obligatoires."),
            ("", "La ligne 2 est un EXEMPLE : remplacez-la par vos données."), ("", "")]
    rows += [(n + (" *" if r else ""), h) for n, r, h in spec["cols"]]
    for i, (a, b) in enumerate(rows, 1):
        help_ws.cell(row=i, column=1, value=a).font = Font(bold=True)
        help_ws.cell(row=i, column=2, value=b).alignment = Alignment(wrap_text=True)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Lecture et vérification
# ---------------------------------------------------------------------------
def read_rows(file_storage):
    name = (file_storage.filename or "").lower()
    data = file_storage.read()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    elif name.endswith(".csv") or name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t") if text.strip() else csv.excel
        rows = [r for r in csv.reader(io.StringIO(text), dialect)]
    else:
        raise ValueError("Envoyez un fichier Excel (.xlsx) ou CSV.")
    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        raise ValueError("Le fichier est vide.")
    return rows


def _norm(text):
    import unicodedata

    t = str(text or "").replace("œ", "oe").replace("Œ", "oe")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return " ".join(t.replace("*", "").split())


def _date(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, Date):
        return v.isoformat()
    s = str(v or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # date Excel en nombre
        from datetime import timedelta

        return (Date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    return None


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    return parse_num(v)


def _txt(v, n=120):
    return " ".join(str(v if v is not None else "").split())[:n]


def check(kind, rows):
    """Retourne (lignes vérifiées, colonnes manquantes). Chaque ligne : {n, values, ok, errors, data, label}."""
    spec = KINDS[kind]
    header = [_norm(h) for h in rows[0]]
    wanted = [_norm(c[0]) for c in spec["cols"]]
    idx = {}
    for i, w in enumerate(wanted):
        if w in header:
            idx[spec["cols"][i][0]] = header.index(w)
    missing = [c[0] for c in spec["cols"] if c[1] and c[0] not in idx]
    if missing:
        return [], missing
    lots = {r["name"].lower(): r for r in query("SELECT * FROM lots")}
    accounts = {r["name"].lower(): r for r in query("SELECT * FROM accounts")}
    seen = set()
    out = []
    for n, raw in enumerate(rows[1:MAX_ROWS + 1], 2):
        get = lambda col: raw[idx[col]] if col in idx and idx[col] < len(raw) else None  # noqa: E731
        errors, data = [], {}
        d = _date(get("Date"))
        if not d:
            errors.append("date illisible")
        elif d > today():
            errors.append("date dans le futur")
        data["date"] = d
        if kind in ("oeufs", "mortalite") or (kind in ("ventes", "depenses") and _txt(get("Lot"))):
            lot = lots.get(_txt(get("Lot")).lower())
            if lot is None:
                errors.append(f"lot « {_txt(get('Lot'))} » inconnu")
            elif d and d < lot["arrival_date"]:
                errors.append(f"avant l'arrivée du lot ({date_fr(lot['arrival_date'])})")
            data["lot_id"] = lot["id"] if lot else None
            data["lot"] = lot["name"] if lot else ""
        if kind == "oeufs":
            good, broken = _num(get("Bons œufs")), _num(get("Cassés")) or 0
            if good is None or good < 0:
                errors.append("nombre de bons œufs manquant")
            if broken < 0:
                errors.append("cassés négatif")
            data.update(good=int(good or 0), broken=int(broken), notes=_txt(get("Remarque"), 150))
            key = (data.get("lot_id"), d)
            if key in seen:
                errors.append("même lot et même jour deux fois dans le fichier")
            elif data.get("lot_id") and d and query("SELECT 1 FROM egg_collections WHERE lot_id = ? AND date = ?", key, one=True):
                errors.append("ce jour est déjà saisi pour ce lot")
            seen.add(key)
            label = f"{data.get('lot', '?')} : {data['good']} bons œufs, {data['broken']} cassés"
        elif kind == "mortalite":
            q = _num(get("Nombre de morts"))
            if q is None or q <= 0 or int(q) != q:
                errors.append("nombre de morts invalide")
            data.update(quantity=int(q or 0), notes=_txt(get("Raison"), 150))
            label = f"{data.get('lot', '?')} : {data['quantity']} mort(s)" + (f" ({data['notes']})" if data["notes"] else "")
        elif kind == "depenses":
            amount = _num(get("Montant"))
            if amount is None or amount <= 0:
                errors.append("montant invalide")
            acc_name = _txt(get("Compte"), 60)
            acc = accounts.get(acc_name.lower()) if acc_name else None
            data.update(category=_txt(get("Poste"), 60) or "Divers", label=_txt(get("Détail")), amount=amount or 0,
                        account=acc_name, account_id=acc["id"] if acc else None, beneficiary=_txt(get("Payé à"), 80),
                        notes=_txt(get("Remarque"), 300), lot_id=data.get("lot_id"))
            if not _txt(get("Poste")):
                errors.append("poste manquant")
            label = f"{data['category']} {data['label']} : {fmt_money(data['amount'])}" + (
                f" · {acc_name}" + (" (nouveau compte)" if not acc else "") if acc_name else " · à payer")
        else:  # ventes
            product = _txt(get("Produit"), 60) or "Œufs"
            eggs = _norm(product).startswith("oeuf")
            qty, price = _num(get("Quantité")), _num(get("Prix unitaire"))
            unit = _norm(get("Unité"))
            if qty is None or qty <= 0:
                errors.append("quantité invalide")
            if price is None or price < 0:
                errors.append("prix invalide")
            qty, price = qty or 0, price or 0
            if eggs:
                if unit.startswith("plateau"):
                    n_eggs, unit_price, written = qty * TRAY, price / TRAY, f"{qty:g} plateau(x)"
                elif unit.startswith("oeuf") or not unit:
                    n_eggs, unit_price, written = qty, price, f"{qty:g} œuf(s)"
                else:
                    errors.append("pour les œufs, l'unité doit être « plateau » ou « œuf »")
                    n_eggs, unit_price, written = qty, price, ""
                data.update(is_eggs=1, quantity=n_eggs, unit="œuf", unit_price=unit_price, input_qty=written)
            else:
                data.update(is_eggs=0, quantity=qty, unit=_txt(get("Unité"), 20) or "unité", unit_price=price,
                            input_qty=f"{qty:g} {_txt(get('Unité'), 20) or 'unité'}")
            total = round(qty * price, 2)
            paid = _num(get("Payé"))
            paid = total if paid is None else paid
            if paid < 0 or paid > total + 0.5:
                errors.append("payé plus grand que le total")
            acc_name = _txt(get("Compte"), 60)
            acc = accounts.get(acc_name.lower()) if acc_name else None
            if paid > 0 and not acc_name:
                acc_name = "Caisse de la ferme"
                acc = accounts.get(acc_name.lower())
            data.update(product=product, total=total, paid=paid, account=acc_name, account_id=acc["id"] if acc else None,
                        client=_txt(get("Client"), 80), notes=_txt(get("Remarque"), 200), lot_id=data.get("lot_id"))
            label = (f"{data['client'] or 'Client de passage'} : {data['input_qty']} {'' if eggs else product} = {fmt_money(total)}"
                     + (f" · payé {fmt_money(paid)}" if paid < total - 0.5 else ""))
        out.append({"n": n, "values": [("" if c is None else (c.strftime("%d/%m/%Y") if isinstance(c, (datetime, Date)) else c))
                                       for c in raw[:len(header)]],
                    "ok": not errors, "errors": errors, "data": data, "label": label if not errors else ""})
    return out, []


def _account_id(conn, name, cache):
    if not name:
        return None
    key = name.lower()
    if key not in cache:
        row = conn.execute("SELECT id FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        cache[key] = row["id"] if row else conn.execute("INSERT INTO accounts (name, kind, sort) VALUES (?, 'autre', 99)", (name,)).lastrowid
    return cache[key]


def _client_id(conn, name, cache):
    if not name:
        return None
    key = name.lower()
    if key not in cache:
        row = conn.execute("SELECT id FROM clients WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        cache[key] = row["id"] if row else conn.execute("INSERT INTO clients (name, created_at) VALUES (?, ?)", (name, now_utc())).lastrowid
    return cache[key]


def save(kind, rows, filename):
    uid, now = g.user["id"], now_utc()
    good = [r["data"] for r in rows if r["ok"]]
    acc_cache, cli_cache = {}, {}
    with transaction() as conn:
        imp = conn.execute("INSERT INTO imports (kind, filename, rows, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                           (kind, filename[:120], len(good), uid, now)).lastrowid
        for d in good:
            if kind == "oeufs":
                conn.execute("""INSERT INTO egg_collections (lot_id, date, good, broken, notes, import_id, created_by, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (d["lot_id"], d["date"], d["good"], d["broken"], d["notes"], imp, uid, now))
            elif kind == "mortalite":
                conn.execute("""INSERT INTO lot_events (lot_id, date, kind, quantity, notes, import_id, created_by, created_at)
                                VALUES (?, ?, 'mort', ?, ?, ?, ?, ?)""", (d["lot_id"], d["date"], -d["quantity"], d["notes"], imp, uid, now))
            elif kind == "depenses":
                acc = _account_id(conn, d["account"], acc_cache)
                exp = conn.execute("""INSERT INTO expenses (date, category, label, amount, lot_id, beneficiary, status, paid_date, account_id,
                                      notes, import_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                   (d["date"], d["category"], d["label"], d["amount"], d["lot_id"], d["beneficiary"],
                                    "paye" if acc else "a_payer", d["date"] if acc else "", acc, d["notes"], imp, uid, now)).lastrowid
                if acc:
                    conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, import_id, created_by, created_at)
                                    VALUES (?, ?, ?, 'depense', ?, 'expense', ?, ?, ?, ?)""",
                                 (d["date"], acc, -d["amount"], f"Dépense : {d['category']} {d['label']}".strip(), exp, imp, uid, now))
            else:
                acc = _account_id(conn, d["account"], acc_cache) if d["paid"] > 0 else None
                cli = _client_id(conn, d["client"], cli_cache)
                sale = conn.execute("""INSERT INTO sales (date, client_id, product, is_eggs, quantity, unit, input_qty, unit_price, total, paid,
                                       account_id, lot_id, notes, import_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                    (d["date"], cli, d["product"], d["is_eggs"], d["quantity"], d["unit"], d["input_qty"], d["unit_price"],
                                     d["total"], d["paid"], acc, d["lot_id"], d["notes"], imp, uid, now)).lastrowid
                if acc:
                    conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, import_id, created_by, created_at)
                                    VALUES (?, ?, ?, 'vente', ?, 'sale', ?, ?, ?, ?)""",
                                 (d["date"], acc, d["paid"], f"Vente n°{sale} (import)", sale, imp, uid, now))
    return imp, len(good)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@bp.route("/")
@require("imports", VIEW)
def index():
    history = query("""SELECT i.*, COALESCE(NULLIF(u.full_name, ''), u.username) AS username FROM imports i
                       LEFT JOIN users u ON u.id = i.created_by ORDER BY i.id DESC LIMIT 30""")
    return render_template("imports/index.html", kinds=KINDS, history=history, xlsx=has_xlsx())


@bp.route("/modele/<kind>")
@require("imports", VIEW)
def template(kind):
    if kind not in KINDS:
        abort(404)
    if not has_xlsx():
        spec = KINDS[kind]
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow([c[0] for c in spec["cols"]])
        w.writerow(spec["example"])
        return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")), as_attachment=True, download_name=f"modele-{kind}.csv",
                         mimetype="text/csv")
    return send_file(template_bytes(kind), as_attachment=True, download_name=f"modele-{kind}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/verifier", methods=["POST"])
@require("imports", EDIT)
def verify():
    kind = request.form.get("kind", "")
    file = request.files.get("file")
    if kind not in KINDS or not file or not file.filename:
        flash("Choisissez la sorte d'import et le fichier.", "error")
        return redirect(url_for("imports.index"))
    try:
        rows = read_rows(file)
        checked, missing = check(kind, rows)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("imports.index"))
    except Exception:  # fichier abîmé ou d'un autre format
        flash("Impossible de lire ce fichier. Utilisez le modèle Excel du logiciel (.xlsx).", "error")
        return redirect(url_for("imports.index"))
    if missing:
        flash("Colonnes manquantes : " + ", ".join(missing) + ". Utilisez le modèle Excel du logiciel.", "error")
        return redirect(url_for("imports.index"))
    if len(rows) - 1 > MAX_ROWS:
        flash(f"Seules les {MAX_ROWS} premières lignes sont prises. Coupez le fichier en plusieurs morceaux.", "info")
    token = secrets.token_hex(12)
    with open(os.path.join(_tmp_dir(), f"{token}.json"), "w", encoding="utf-8") as fh:
        json.dump({"kind": kind, "filename": file.filename, "rows": checked, "user": g.user["id"]}, fh)
    return render_template("imports/preview.html", kind=kind, spec=KINDS[kind], rows=checked, token=token, filename=file.filename,
                           header=[str(h or "") for h in rows[0]], ok=sum(1 for r in checked if r["ok"]),
                           bad=sum(1 for r in checked if not r["ok"]))


@bp.route("/confirmer", methods=["POST"])
@require("imports", EDIT)
def confirm():
    token = request.form.get("token", "")
    if not token.isalnum():
        abort(400)
    path = os.path.join(_tmp_dir(), f"{token}.json")
    if not os.path.exists(path):
        flash("Cet import a expiré : renvoyez le fichier.", "error")
        return redirect(url_for("imports.index"))
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    os.remove(path)
    if data.get("user") != g.user["id"]:
        abort(403)
    imp, n = save(data["kind"], data["rows"], data["filename"])
    spec = KINDS[data["kind"]]
    log_activity("Import Excel", f"{spec['title']} : {n} ligne(s) ({data['filename']})")
    notify("", None, f"📥 Import Excel « {spec['title']} » : {n} ligne(s) ajoutée(s) depuis le fichier {data['filename']}.")
    flash(f"Import terminé : {n} ligne(s) enregistrée(s). En cas d'erreur, « Annuler cet import » efface tout.", "success")
    return redirect(url_for("imports.index"))


@bp.route("/<int:import_id>/annuler", methods=["POST"])
@require("imports", MANAGE)
def undo(import_id):
    imp = query("SELECT * FROM imports WHERE id = ?", (import_id,), one=True)
    if imp is None:
        abort(404)
    if imp["status"] == "annule":
        flash("Cet import est déjà annulé.", "info")
        return redirect(url_for("imports.index"))
    with transaction() as conn:
        conn.execute("DELETE FROM cash_movements WHERE import_id = ?", (import_id,))
        for t in ("egg_collections", "lot_events", "expenses"):
            conn.execute(f"DELETE FROM {t} WHERE import_id = ?", (import_id,))
        sale_ids = [r["id"] for r in conn.execute("SELECT id FROM sales WHERE import_id = ?", (import_id,)).fetchall()]
        for sid in sale_ids:  # encaissements ajoutés plus tard sur ces ventes
            for p in conn.execute("SELECT id FROM sale_payments WHERE sale_id = ?", (sid,)).fetchall():
                conn.execute("DELETE FROM cash_movements WHERE ref_type = 'sale_payment' AND ref_id = ?", (p["id"],))
            conn.execute("DELETE FROM sale_payments WHERE sale_id = ?", (sid,))
        conn.execute("DELETE FROM sales WHERE import_id = ?", (import_id,))
        conn.execute("UPDATE imports SET status = 'annule' WHERE id = ?", (import_id,))
    log_activity("Import annulé", f"n°{import_id} ({imp['filename']})")
    notify("", None, f"↩️ Import Excel n°{import_id} annulé ({imp['filename']}) : tout ce qu'il avait ajouté est effacé.")
    flash("Import annulé : tout ce qu'il avait ajouté est effacé.", "success")
    return redirect(url_for("imports.index"))


__all__ = ["bp", "KINDS", "check", "save", "template_bytes"]
