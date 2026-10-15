"""Personnel & salaires : les employés, leurs absences, les avances, les primes et les salaires payés.

Règles :
- Salaire AU MOIS : le salaire complet est dû chaque mois (au prorata le premier et le dernier mois).
  Les absences sont notées pour information.
- Salaire À LA JOURNÉE : dû = jours travaillés × prix d'une journée.
  Jours travaillés = jours du mois où la personne était employée − ses absences.
- Une AVANCE est une partie du salaire payée plus tôt : elle diminue le reste à payer du mois.
- Une PRIME s'ajoute en plus du salaire.
- Chaque paiement sort d'une caisse et compte comme charge commune, partagée entre les lots (voir app/costs.py).
"""
import calendar
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, can_correct, require
from .stock import accounts, parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("personnel", __name__, url_prefix="/personnel")

PAY_KINDS = {"salaire": "Salaire", "avance": "Avance", "prime": "Prime"}
JOBS = ["Soigneur / soigneuse", "Gardien", "Ramasseur d'œufs", "Vendeur / livreur", "Chauffeur", "Responsable de ferme", "Ouvrier provenderie"]


def _d(v):
    return Date.fromisoformat(v)


def month_bounds(month):
    y, m = int(month[:4]), int(month[5:7])
    return Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])


def month_label(month):
    from .expenses import _mlabel

    return _mlabel(month)


def employed_days(emp, month):
    """Jours du mois où la personne était employée (jusqu'à aujourd'hui pour le mois en cours)."""
    first, last = month_bounds(month)
    last = min(last, _d(today()))
    if emp["start_date"]:
        first = max(first, _d(emp["start_date"]))
    if emp["end_date"]:
        last = min(last, _d(emp["end_date"]))
    return max(0, (last - first).days + 1)


def month_sheet(emp, month):
    first, last = month_bounds(month)
    full_days = (last - first).days + 1
    days_in = employed_days(emp, month)
    absent = query("SELECT COALESCE(SUM(days), 0) AS d FROM staff_absences WHERE employee_id = ? AND substr(date, 1, 7) = ?",
                   (emp["id"], month), one=True)["d"]
    pays = {k: 0.0 for k in PAY_KINDS}
    for p in query("SELECT kind, SUM(amount) AS t FROM staff_payments WHERE employee_id = ? AND month = ? GROUP BY kind",
                   (emp["id"], month)):
        pays[p["kind"]] = p["t"] or 0
    if emp["pay_type"] == "jour":
        worked = max(0, days_in - absent)
        due = worked * (emp["salary"] or 0)
        rule = f"{worked:g} jour(s) travaillé(s) × {fmt_money(emp['salary'])}"
    else:
        worked = max(0, days_in - absent)
        # au prorata si la personne est arrivée ou partie pendant le mois
        employed_full = full_days
        if emp["start_date"]:
            employed_full -= max(0, (_d(emp["start_date"]) - first).days)
        if emp["end_date"] and _d(emp["end_date"]) < last:
            employed_full -= (last - _d(emp["end_date"])).days
        employed_full = max(0, min(full_days, employed_full))
        due = (emp["salary"] or 0) * employed_full / full_days
        rule = "salaire du mois" if employed_full == full_days else f"salaire × {employed_full}/{full_days} jours (arrivé/parti en cours de mois)"
    paid = pays["salaire"] + pays["avance"]
    return {"due": due, "rule": rule, "absent": absent, "worked": worked, "salaire": pays["salaire"], "avance": pays["avance"],
            "prime": pays["prime"], "paid": paid, "rest": due - paid, "total_paid": paid + pays["prime"]}


def employees(active_only=True, month=None):
    rows = query(f"SELECT * FROM employees {'WHERE active = 1' if active_only else ''} ORDER BY active DESC, name")
    if month:
        first, last = month_bounds(month)
        rows = [r for r in rows if (not r["start_date"] or r["start_date"] <= last.isoformat())
                and (not r["end_date"] or r["end_date"] >= first.isoformat())]
    return rows


def unpaid_previous():
    prev = (_d(today()).replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    rest = 0.0
    names = []
    for e in employees(False, prev):
        s = month_sheet(e, prev)
        if s["rest"] > 0.5:
            rest += s["rest"]
            names.append(e["name"])
    return prev, rest, names


def alerts():
    if _d(today()).day < 5:
        return []
    prev, rest, names = unpaid_previous()
    if rest <= 0.5:
        return []
    return [{"level": "warning", "title": f"👷 Salaires de {month_label(prev)} pas entièrement payés",
             "text": f"Reste {fmt_money(rest)} ({', '.join(names[:4])}{'…' if len(names) > 4 else ''}).",
             "url": url_for("personnel.index", mois=prev)}]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@bp.route("/")
@require("personnel", VIEW)
def index():
    month = request.args.get("mois", "")
    if len(month) != 7 or not valid_date(month + "-01"):
        month = today()[:7]
    staff = [{"e": e, "s": month_sheet(e, month)} for e in employees(False, month)]
    from .expenses import _months

    keys = _months(12)
    per = {r["k"]: r["t"] for r in query("SELECT month AS k, SUM(amount) AS t FROM staff_payments WHERE month >= ? GROUP BY k",
                                         (keys[0],))}
    first = next((i for i, k in enumerate(keys) if per.get(k)), len(keys) - 1)
    keys = keys[first:] or keys[-1:]
    chart = {"labels": [month_label(k) for k in keys], "bars": [round(per.get(k, 0)) for k in keys], "line": [None] * len(keys),
             "unit": "Ar", "barLabel": "Salaires payés", "barColor": "#7b3fbf"}
    tot = {"due": sum(x["s"]["due"] for x in staff), "paid": sum(x["s"]["paid"] for x in staff),
           "prime": sum(x["s"]["prime"] for x in staff), "rest": sum(max(0, x["s"]["rest"]) for x in staff),
           "count": sum(1 for x in staff if x["e"]["active"])}
    months = list(reversed(_months(12)))
    return render_template("personnel/index.html", staff=staff, month=month, mlabel=month_label, chart=chart,
                           has_chart=any(per.values()), tot=tot, months=months, accounts=accounts(), today=today(),
                           kinds=PAY_KINDS, jobs=JOBS)


def _emp_form(existing=None):
    f = request.form
    v = {"name": " ".join(f.get("name", "").split())[:80], "job": " ".join(f.get("job", "").split())[:60],
         "phone": " ".join(f.get("phone", "").split())[:30], "pay_type": "jour" if f.get("pay_type") == "jour" else "mois",
         "salary": parse_num(f.get("salary")), "start_date": f.get("start_date") or "", "notes": f.get("notes", "").strip()[:300]}
    errors = []
    if len(v["name"]) < 2:
        errors.append("Écrivez le nom de l'employé.")
    if v["salary"] is None or v["salary"] < 0:
        errors.append("Écrivez le salaire (par mois ou par jour).")
    if v["start_date"] and not valid_date(v["start_date"]):
        errors.append("Date d'arrivée invalide.")
    return v, errors


@bp.route("/employe/nouveau", methods=["GET", "POST"])
@require("personnel", EDIT)
def employee_new():
    v = {"name": "", "job": "", "phone": "", "pay_type": "mois", "salary": "", "start_date": today(), "notes": ""}
    if request.method == "POST":
        v, errors = _emp_form()
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            emp_id = execute("""INSERT INTO employees (name, job, phone, pay_type, salary, start_date, notes, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (v["name"], v["job"], v["phone"], v["pay_type"], v["salary"],
                                                                      v["start_date"], v["notes"], now_utc()))
            log_activity("Employé ajouté", v["name"])
            notify("employee", emp_id, f"👷 Nouvel employé : {v['name']}" + (f" ({v['job']})" if v["job"] else "")
                   + f" · {fmt_money(v['salary'])} par {'jour' if v['pay_type'] == 'jour' else 'mois'}.")
            flash("Employé enregistré.", "success")
            return redirect(url_for("personnel.employee", employee_id=emp_id))
    return render_template("personnel/employee_form.html", v=v, item=None, jobs=JOBS)


def _emp(employee_id):
    row = query("SELECT * FROM employees WHERE id = ?", (employee_id,), one=True)
    if row is None:
        abort(404)
    return row


@bp.route("/employe/<int:employee_id>")
@require("personnel", VIEW)
def employee(employee_id):
    emp = _emp(employee_id)
    pays = query("""SELECT p.*, a.name AS account FROM staff_payments p LEFT JOIN accounts a ON a.id = p.account_id
                    WHERE p.employee_id = ? ORDER BY p.date DESC, p.id DESC""", (employee_id,))
    absences = query("SELECT * FROM staff_absences WHERE employee_id = ? ORDER BY date DESC", (employee_id,))
    from .expenses import _months

    months = [m for m in reversed(_months(12)) if employed_days(emp, m) > 0]
    sheets = [{"month": m, "s": month_sheet(emp, m)} for m in months]
    return render_template("personnel/employee.html", emp=emp, pays=pays, absences=absences, sheets=sheets, mlabel=month_label,
                           kinds=PAY_KINDS, accounts=accounts(), today=today())


@bp.route("/employe/<int:employee_id>/modifier", methods=["GET", "POST"])
@require("personnel", MANAGE)
def employee_edit(employee_id):
    emp = _emp(employee_id)
    v = dict(emp)
    if request.method == "POST":
        v, errors = _emp_form()
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            execute("UPDATE employees SET name = ?, job = ?, phone = ?, pay_type = ?, salary = ?, start_date = ?, notes = ? WHERE id = ?",
                    (v["name"], v["job"], v["phone"], v["pay_type"], v["salary"], v["start_date"], v["notes"], employee_id))
            if (emp["salary"], emp["pay_type"]) != (v["salary"], v["pay_type"]):
                notify("employee", employee_id, f"✏️ Salaire de {v['name']} : {fmt_money(emp['salary'])} → {fmt_money(v['salary'])} "
                       f"par {'jour' if v['pay_type'] == 'jour' else 'mois'}.")
            log_activity("Employé modifié", v["name"])
            flash("Fiche mise à jour.", "success")
            return redirect(url_for("personnel.employee", employee_id=employee_id))
    return render_template("personnel/employee_form.html", v=v, item=emp, jobs=JOBS)


@bp.route("/employe/<int:employee_id>/depart", methods=["POST"])
@require("personnel", MANAGE)
def employee_leave(employee_id):
    emp = _emp(employee_id)
    if emp["active"]:
        end = request.form.get("end_date") or today()
        if not valid_date(end):
            flash("Date invalide.", "error")
            return redirect(url_for("personnel.employee", employee_id=employee_id))
        execute("UPDATE employees SET active = 0, end_date = ? WHERE id = ?", (end, employee_id))
        notify("employee", employee_id, f"👋 {emp['name']} ne travaille plus à la ferme depuis le {date_fr(end)}.")
        flash("Départ enregistré. Son historique est gardé.", "success")
    else:
        execute("UPDATE employees SET active = 1, end_date = '' WHERE id = ?", (employee_id,))
        flash("L'employé est de nouveau actif.", "success")
    return redirect(url_for("personnel.employee", employee_id=employee_id))


@bp.route("/paiement", methods=["POST"])
@require("personnel", EDIT)
def payment_new():
    f = request.form
    emp = _emp(f.get("employee_id", type=int) or 0)
    kind = f.get("kind") if f.get("kind") in PAY_KINDS else "salaire"
    date = f.get("date") or today()
    month = f.get("month") or date[:7]
    amount = parse_num(f.get("amount"))
    days = parse_num(f.get("days"))
    account = query("SELECT * FROM accounts WHERE id = ?", (f.get("account_id") or 0,), one=True)
    back = request.form.get("next") or url_for("personnel.index", mois=month)
    if not valid_date(date) or date > today() or len(month) != 7 or not valid_date(month + "-01"):
        flash("Date ou mois invalide.", "error")
        return redirect(back)
    if amount is None or amount <= 0 or account is None:
        flash("Écrivez le montant et choisissez la caisse.", "error")
        return redirect(back)
    notes = " ".join(f.get("notes", "").split())[:200]
    with transaction() as conn:
        cur = conn.execute("""INSERT INTO staff_payments (employee_id, date, month, kind, amount, days, account_id, notes, created_by, created_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (emp["id"], date, month, kind, amount, days, account["id"], notes, g.user["id"], now_utc()))
        conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                        VALUES (?, ?, ?, 'salaire', ?, 'staff_payment', ?, ?, ?)""",
                     (date, account["id"], -amount, f"{PAY_KINDS[kind]} {emp['name']} ({month_label(month)})", cur.lastrowid,
                      g.user["id"], now_utc()))
    s = month_sheet(emp, month)
    log_activity(PAY_KINDS[kind], f"{emp['name']} {fmt_money(amount)} ({month})")
    notify("employee", emp["id"], f"👷 {PAY_KINDS[kind]} payé(e) à {emp['name']} : {fmt_money(amount)} pour {month_label(month)} "
           f"(caisse « {account['name']} »)." + (f" Reste à payer pour ce mois : {fmt_money(s['rest'])}." if s["rest"] > 0.5 else ""))
    flash(f"{PAY_KINDS[kind]} enregistré(e). " + (f"Reste à payer pour {month_label(month)} : {fmt_money(s['rest'])}." if s["rest"] > 0.5
                                                   else f"Le salaire de {month_label(month)} est entièrement payé ✅."), "success")
    return redirect(back)


@bp.route("/paiement/<int:pay_id>/supprimer", methods=["POST"])
@require("personnel", EDIT)
def payment_delete(pay_id):
    p = query("SELECT * FROM staff_payments WHERE id = ?", (pay_id,), one=True)
    if p is None:
        abort(404)
    if not can_correct("personnel", p):
        abort(403)
    emp = _emp(p["employee_id"])
    with transaction() as conn:
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'staff_payment' AND ref_id = ?", (pay_id,))
        conn.execute("DELETE FROM staff_payments WHERE id = ?", (pay_id,))
    notify("employee", emp["id"], f"↩️ Paiement annulé : {PAY_KINDS.get(p['kind'])} de {fmt_money(p['amount'])} à {emp['name']} du {date_fr(p['date'])}.")
    flash("Paiement annulé : l'argent est revenu dans la caisse.", "success")
    return redirect(request.form.get("next") or url_for("personnel.employee", employee_id=emp["id"]))


@bp.route("/absence", methods=["POST"])
@require("personnel", EDIT)
def absence_new():
    f = request.form
    emp = _emp(f.get("employee_id", type=int) or 0)
    date = f.get("date") or today()
    days = parse_num(f.get("days"), 1)
    if not valid_date(date) or days is None or days <= 0 or days > 31:
        flash("Date ou nombre de jours invalide.", "error")
    else:
        execute("INSERT INTO staff_absences (employee_id, date, days, reason, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (emp["id"], date, days, " ".join(f.get("reason", "").split())[:150], g.user["id"], now_utc()))
        flash(f"Absence enregistrée ({days:g} jour(s))." + (" Elle diminue le salaire (payé à la journée)." if emp["pay_type"] == "jour" else ""), "success")
    return redirect(request.form.get("next") or url_for("personnel.employee", employee_id=emp["id"]))


@bp.route("/absence/<int:abs_id>/supprimer", methods=["POST"])
@require("personnel", EDIT)
def absence_delete(abs_id):
    a = query("SELECT * FROM staff_absences WHERE id = ?", (abs_id,), one=True)
    if a is None:
        abort(404)
    if not can_correct("personnel", a):
        abort(403)
    execute("DELETE FROM staff_absences WHERE id = ?", (abs_id,))
    flash("Absence supprimée.", "success")
    return redirect(url_for("personnel.employee", employee_id=a["employee_id"]))


__all__ = ["bp", "alerts", "month_sheet", "employees"]
