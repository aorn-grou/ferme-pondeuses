"""Suivi sanitaire : vaccinations, traitements, vitamines… avec alertes.

- Un programme sanitaire dit : « au jour d'âge N, faire telle chose » (J1 = jour d'arrivée des poussins).
- Quand un lot suit un programme, chaque étape devient un soin prévu à une date précise.
- On peut aussi ajouter un soin à la main (maladie, traitement imprévu…).
- Alerte 7 jours avant (J-7), puis le jour J, puis « en retard » : l'alerte reste tant que personne n'a
  confirmé que le soin est fait. Les associés sont prévenus dans la Discussion (et par e-mail si configuré).
"""
from datetime import date as Date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import execute, now_utc, query, transaction
from .discussion import notify
from .security import EDIT, MANAGE, VIEW, can, can_correct, require
from .stock import accounts, parse_num, today, valid_date
from .utils import date_fr, fmt_money, log_activity

bp = Blueprint("sanitaire", __name__, url_prefix="/sanitaire")

WARN_DAYS = 7
KINDS = {
    "vaccin": ("💉", "Vaccination"),
    "traitement": ("💊", "Traitement"),
    "vitamines": ("🧪", "Vitamines / anti-stress"),
    "vermifuge": ("🪱", "Vermifuge"),
    "desinfection": ("🧴", "Désinfection / hygiène"),
    "autre": ("🩺", "Autre soin"),
}
METHODS = ["Eau de boisson", "Goutte dans l'œil", "Goutte dans le nez", "Injection", "Transfixion de l'aile",
           "Dans la provende", "Pulvérisation", "Au couvoir"]

# Exemple de programme, à adapter avec votre vétérinaire (jour d'âge, quoi, type, comment)
EXAMPLE = [
    (1, "Marek (souvent fait au couvoir)", "vaccin", "Au couvoir"),
    (1, "Vitamines + anti-stress à l'arrivée", "vitamines", "Eau de boisson"),
    (7, "Newcastle + Bronchite infectieuse", "vaccin", "Goutte dans l'œil"),
    (14, "Gumboro", "vaccin", "Eau de boisson"),
    (21, "Newcastle (rappel)", "vaccin", "Eau de boisson"),
    (28, "Gumboro (rappel)", "vaccin", "Eau de boisson"),
    (42, "Variole aviaire", "vaccin", "Transfixion de l'aile"),
    (56, "Newcastle (rappel)", "vaccin", "Eau de boisson"),
    (70, "Vermifuge", "vermifuge", "Eau de boisson"),
    (112, "Newcastle + Bronchite + EDS (avant la ponte)", "vaccin", "Injection"),
    (119, "Vitamines avant la ponte", "vitamines", "Eau de boisson"),
    (182, "Newcastle (rappel en ponte)", "vaccin", "Eau de boisson"),
    (252, "Newcastle (rappel en ponte)", "vaccin", "Eau de boisson"),
    (322, "Newcastle (rappel en ponte)", "vaccin", "Eau de boisson"),
    (392, "Newcastle (rappel en ponte)", "vaccin", "Eau de boisson"),
]


def _d(value):
    return Date.fromisoformat(value)


def date_of_day(lot, day):
    """Date du jour d'âge `day` du lot (J1 = jour d'arrivée pour des poussins d'un jour)."""
    return _d(lot["arrival_date"]) + timedelta(days=day - 1 - (lot["age_days_at_arrival"] or 0))


def age_day(lot, on=None):
    from .lots import lot_age_days

    return lot_age_days(lot, on) + 1


def describe_day(day):
    week = (day - 1) // 7 + 1
    return f"J{day} · semaine {week}"


# ---------------------------------------------------------------------------
# Programmes → soins prévus
# ---------------------------------------------------------------------------
def program_items(program_id):
    return query("SELECT * FROM health_program_items WHERE program_id = ? ORDER BY day_age, id", (program_id,))


def schedule_lot(conn, lot):
    """Crée (ou met à jour) les soins prévus d'un lot d'après son programme sanitaire.
    Les soins déjà faits ou annulés ne sont jamais touchés."""
    conn.execute("DELETE FROM health_events WHERE lot_id = ? AND item_id IS NOT NULL AND status = 'prevu'", (lot["id"],))
    if not lot["health_program_id"] or lot["status"] != "actif":
        return 0
    done = {r[0] for r in conn.execute("SELECT item_id FROM health_events WHERE lot_id = ? AND item_id IS NOT NULL",
                                       (lot["id"],)).fetchall()}
    items = conn.execute("SELECT * FROM health_program_items WHERE program_id = ?", (lot["health_program_id"],)).fetchall()
    reform = date_of_day(lot, (lot["reform_week"] or 80) * 7 + 1)
    n = 0
    for it in items:
        if it["id"] in done:
            continue
        due = date_of_day(lot, it["day_age"])
        if due > reform:
            continue
        conn.execute("""INSERT INTO health_events (lot_id, item_id, due_date, title, kind, method, product, status, notes,
                        created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'prevu', ?, ?, ?)""",
                     (lot["id"], it["id"], due.isoformat(), it["title"], it["kind"], it["method"], it["product"] or "",
                      it["notes"] or "", g.user["id"] if g.get("user") else None, now_utc()))
        n += 1
    return n


def reschedule_program(program_id):
    with transaction() as conn:
        for lot in conn.execute("SELECT * FROM lots WHERE health_program_id = ? AND status = 'actif'", (program_id,)).fetchall():
            schedule_lot(conn, lot)


def schedule_lot_id(lot_id):
    with transaction() as conn:
        lot = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
        if lot:
            schedule_lot(conn, lot)


# ---------------------------------------------------------------------------
# Soins, alertes, signalements
# ---------------------------------------------------------------------------
def events(where="1 = 1", params=(), order="e.due_date, e.id"):
    rows = query(f"""SELECT e.*, l.name AS lot, l.status AS lot_status, u.full_name AS done_name, u.username AS done_user,
                            a.name AS account
                     FROM health_events e LEFT JOIN lots l ON l.id = e.lot_id
                     LEFT JOIN users u ON u.id = e.done_by LEFT JOIN accounts a ON a.id = e.account_id
                     WHERE {where} ORDER BY {order}""", params)
    t = _d(today())
    out = []
    for r in rows:
        r = dict(r)
        days = (_d(r["due_date"]) - t).days
        r["days"] = days
        if r["status"] == "fait":
            r["state"], r["state_label"] = "done", "Fait ✅"
        elif r["status"] == "annule":
            r["state"], r["state_label"] = "cancel", "Annulé"
        elif days < 0:
            r["state"], r["state_label"] = "late", f"En retard de {-days} jour{'s' if -days > 1 else ''}"
        elif days == 0:
            r["state"], r["state_label"] = "today", "Aujourd'hui"
        elif days <= WARN_DAYS:
            r["state"], r["state_label"] = "soon", f"Dans {days} jour{'s' if days > 1 else ''}"
        else:
            r["state"], r["state_label"] = "later", f"Dans {days} jours"
        r["emoji"], r["kind_label"] = KINDS.get(r["kind"], KINDS["autre"])
        out.append(r)
    return out


def pending(active_only=True):
    """Soins à faire : en retard, aujourd'hui, ou dans les 7 prochains jours (non confirmés)."""
    limit = (_d(today()) + timedelta(days=WARN_DAYS)).isoformat()
    where = "e.status = 'prevu' AND e.due_date <= ?"
    if active_only:
        where += " AND (e.lot_id IS NULL OR l.status = 'actif')"
    return events(where, (limit,))


def _who_to_email():
    return [r["email"] for r in query("""SELECT email FROM users WHERE active = 1 AND deleted = 0 AND email != ''
                                         AND role IN ('super_admin', 'admin')""")]


def send_reminders():
    """Prévient les associés une fois à J-7, une fois le jour J, puis une fois s'il y a du retard.
    Appelé au chargement des pages (rapide : seulement les soins proches non confirmés)."""
    if g.get("_health_checked") or g.get("demo_building"):
        return
    g._health_checked = True
    try:
        rows = pending()
    except Exception:  # la table peut ne pas exister pendant une migration
        return
    to_mail = []
    for r in rows:
        stage = "late" if r["days"] < 0 else ("j0" if r["days"] == 0 else "j7")
        sent = set((r["notified"] or "").split(","))
        if stage in sent:
            continue
        who = f"lot « {r['lot']} »" if r["lot"] else "tous les lots"
        when = {"j7": f"prévu le {date_fr(r['due_date'])} (dans {r['days']} jour{'s' if r['days'] > 1 else ''})",
                "j0": "à faire AUJOURD'HUI", "late": f"EN RETARD depuis le {date_fr(r['due_date'])}"}[stage]
        icon = {"j7": "🔔", "j0": "⏰", "late": "🚨"}[stage]
        body = (f"{icon} Suivi sanitaire — {r['emoji']} {r['title']} ({who}) : {when}."
                + (f" Comment : {r['method']}." if r["method"] else "")
                + " Confirmez dans Suivi sanitaire quand c'est fait.")
        notify("lot" if r["lot_id"] else "", r["lot_id"], body)
        sent.add(stage)
        execute("UPDATE health_events SET notified = ? WHERE id = ?", (",".join(sorted(s for s in sent if s)), r["id"]))
        to_mail.append(body)
    if to_mail:
        try:
            from .mailer import email_configured, send_email

            if email_configured():
                for addr in _who_to_email():
                    send_email(addr, "Androfia Farm — rappel du suivi sanitaire", "\n\n".join(to_mail))
        except Exception:  # un e-mail raté ne doit jamais bloquer le logiciel
            pass


def alerts():
    """Pour la cloche et le tableau de bord."""
    items = []
    for r in pending():
        who = r["lot"] or "tous les lots"
        if r["state"] == "late":
            level, title = "critical", f"{r['emoji']} {r['title']} : {r['state_label'].lower()}"
        elif r["state"] == "today":
            level, title = "critical", f"{r['emoji']} {r['title']} : aujourd'hui"
        else:
            level, title = "warning", f"{r['emoji']} {r['title']} : {r['state_label'].lower()}"
        items.append({"level": level, "title": title, "text": f"{who} · prévu le {date_fr(r['due_date'])}. À confirmer quand c'est fait.",
                      "url": url_for("sanitaire.index") + f"#soin-{r['id']}"})
    return items


def lot_health_cost(lot_id):
    return query("SELECT COALESCE(SUM(cost), 0) AS c FROM health_events WHERE lot_id = ? AND status = 'fait'",
                 (lot_id,), one=True)["c"]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@bp.route("/")
@require("sanitaire", VIEW)
def index():
    lot_id = request.args.get("lot", type=int)
    where, params = "1 = 1", []
    if lot_id:
        where, params = "e.lot_id = ?", [lot_id]
    todo = [e for e in pending() if not lot_id or e["lot_id"] == lot_id]
    upcoming = events(where + " AND e.status = 'prevu' AND e.due_date > ? AND (e.lot_id IS NULL OR l.status = 'actif')",
                      (*params, (_d(today()) + timedelta(days=WARN_DAYS)).isoformat()))[:40]
    history = events(where + " AND e.status != 'prevu'", tuple(params), order="COALESCE(e.done_date, e.due_date) DESC, e.id DESC")[:80]
    lots = query("SELECT * FROM lots WHERE status = 'actif' ORDER BY arrival_date")
    # graphique : soins faits par mois et mortalité par semaine
    weekly = _mortality_weeks(lot_id)
    return render_template("sanitaire/index.html", todo=todo, upcoming=upcoming, history=history, lots=lots, lot_id=lot_id,
                           kinds=KINDS, methods=METHODS, accounts=accounts(), today=today(), weekly=weekly,
                           programs=query("SELECT * FROM health_programs WHERE active = 1 ORDER BY name"),
                           cost=sum(h["cost"] or 0 for h in history if h["status"] == "fait"))


def _mortality_weeks(lot_id=None, n=12):
    end = _d(today())
    monday = end - timedelta(days=end.weekday())
    starts = [monday - timedelta(weeks=n - 1 - k) for k in range(n)]
    sql = "SELECT date, -SUM(quantity) AS q FROM lot_events WHERE kind = 'mort' AND date >= ?"
    params = [starts[0].isoformat()]
    if lot_id:
        sql += " AND lot_id = ?"
        params.append(lot_id)
    vals = [0] * n
    for r in query(sql + " GROUP BY date", params):
        d = _d(r["date"])
        for k in range(n - 1, -1, -1):
            if d >= starts[k]:
                vals[k] += r["q"] or 0
                break
    done = [0] * n
    sql = "SELECT done_date FROM health_events WHERE status = 'fait' AND done_date >= ?"
    params = [starts[0].isoformat()]
    if lot_id:
        sql += " AND lot_id = ?"
        params.append(lot_id)
    for r in query(sql, params):
        d = _d(r["done_date"])
        for k in range(n - 1, -1, -1):
            if d >= starts[k]:
                done[k] += 1
                break
    labels = [f"{s:%d/%m}" for s in starts]
    return {"chart": {"labels": labels, "bars": vals, "line": [None] * n, "unit": "poules", "barLabel": "Poules mortes",
                      "barColor": "#c0352b", "tips": [f"{c} soin(s) fait(s)" for c in done], "now": labels[-1],
                      "nowLabel": "Cette semaine"}, "total": sum(vals), "has": any(vals) or any(done)}


@bp.route("/soin/nouveau", methods=["POST"])
@require("sanitaire", EDIT)
def event_new():
    f = request.form
    title = " ".join(f.get("title", "").split())[:120]
    due = f.get("due_date", "")
    lot_ids = f.getlist("lot_id") or [""]
    if len(title) < 2 or not valid_date(due):
        flash("Écrivez ce qu'il faut faire et la date.", "error")
        return redirect(request.referrer or url_for("sanitaire.index"))
    kind = f.get("kind") if f.get("kind") in KINDS else "autre"
    created = []
    with transaction() as conn:
        for lid in lot_ids:
            lot = conn.execute("SELECT * FROM lots WHERE id = ?", (lid or 0,)).fetchone() if lid else None
            cur = conn.execute("""INSERT INTO health_events (lot_id, item_id, due_date, title, kind, method, product, status, notes,
                                  created_by, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?, 'prevu', ?, ?, ?)""",
                               (lot["id"] if lot else None, due, title, kind, " ".join(f.get("method", "").split())[:80],
                                " ".join(f.get("product", "").split())[:80], f.get("notes", "").strip()[:300], g.user["id"], now_utc()))
            created.append((cur.lastrowid, lot["name"] if lot else "tous les lots"))
    log_activity("Soin prévu", f"{title} le {date_fr(due)} ({', '.join(n for _, n in created)})")
    notify("", None, f"🩺 Soin ajouté au suivi sanitaire : {KINDS[kind][0]} {title} — {', '.join(n for _, n in created)} — "
           f"prévu le {date_fr(due)}.")
    flash("Soin enregistré. Une alerte apparaîtra 7 jours avant et restera jusqu'à ce que vous confirmiez qu'il est fait.", "success")
    return redirect(url_for("sanitaire.index") + f"#soin-{created[0][0]}")


def _event(event_id):
    rows = events("e.id = ?", (event_id,))
    if not rows:
        abort(404)
    return rows[0]


@bp.route("/soin/<int:event_id>/fait", methods=["POST"])
@require("sanitaire", EDIT)
def event_done(event_id):
    ev = _event(event_id)
    f = request.form
    done = f.get("done_date") or today()
    if not valid_date(done) or done > today():
        flash("Date invalide : le soin ne peut pas être fait dans le futur.", "error")
        return redirect(url_for("sanitaire.index") + f"#soin-{event_id}")
    cost = parse_num(f.get("cost"), 0) or 0
    account = query("SELECT * FROM accounts WHERE id = ?", (f.get("account_id") or 0,), one=True)
    if cost < 0:
        flash("Le coût ne peut pas être négatif.", "error")
        return redirect(url_for("sanitaire.index") + f"#soin-{event_id}")
    if cost > 0 and account is None:
        flash("Indiquez d'où vient l'argent du soin (caisse…), ou laissez le coût vide.", "error")
        return redirect(url_for("sanitaire.index") + f"#soin-{event_id}")
    product = " ".join(f.get("product", "").split())[:80] or ev["product"]
    dose = " ".join(f.get("dose", "").split())[:80]
    notes = " ".join(f.get("notes", "").split())[:300]
    with transaction() as conn:
        conn.execute("""UPDATE health_events SET status = 'fait', done_date = ?, done_by = ?, product = ?, dose = ?, cost = ?,
                        account_id = ?, notes = CASE WHEN ? != '' THEN ? ELSE notes END WHERE id = ?""",
                     (done, g.user["id"], product, dose, cost, account["id"] if account and cost > 0 else None,
                      notes, notes, event_id))
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'health' AND ref_id = ?", (event_id,))
        if cost > 0:
            conn.execute("""INSERT INTO cash_movements (date, account_id, amount, kind, label, ref_type, ref_id, created_by, created_at)
                            VALUES (?, ?, ?, 'sanitaire', ?, 'health', ?, ?, ?)""",
                         (done, account["id"], -cost, f"Soin : {ev['title']}", event_id, g.user["id"], now_utc()))
    late = (_d(done) - _d(ev["due_date"])).days
    log_activity("Soin fait", f"{ev['title']} ({ev['lot'] or 'tous les lots'}) le {date_fr(done)}")
    notify("lot" if ev["lot_id"] else "", ev["lot_id"],
           f"✅ Suivi sanitaire — {ev['emoji']} {ev['title']} ({ev['lot'] or 'tous les lots'}) FAIT le {date_fr(done)}"
           + (f" ({late} jour(s) de retard)" if late > 0 else "") + (f" · produit : {product}" if product else "")
           + (f" · {dose}" if dose else "") + (f" · coût {fmt_money(cost)}" if cost else "") + (f" · « {notes} »" if notes else ""))
    flash("C'est confirmé : le soin est fait. L'alerte est arrêtée et vos associés sont prévenus.", "success")
    return redirect(request.form.get("next") or url_for("sanitaire.index"))


@bp.route("/soin/<int:event_id>/modifier", methods=["POST"])
@require("sanitaire", EDIT)
def event_move(event_id):
    ev = _event(event_id)
    action = request.form.get("action")
    reason = " ".join(request.form.get("reason", "").split())[:200]
    if action == "annuler":
        if not reason:
            flash("Écrivez pourquoi ce soin n'est pas fait (ex. pas nécessaire, vétérinaire a dit…).", "error")
            return redirect(url_for("sanitaire.index") + f"#soin-{event_id}")
        execute("UPDATE health_events SET status = 'annule', done_date = ?, done_by = ?, notes = ? WHERE id = ?",
                (today(), g.user["id"], reason, event_id))
        notify("lot" if ev["lot_id"] else "", ev["lot_id"],
               f"⛔ Suivi sanitaire — {ev['title']} ({ev['lot'] or 'tous les lots'}) ANNULÉ : « {reason} »")
        flash("Soin annulé (la raison est gardée et vos associés sont prévenus).", "success")
    elif action == "reporter":
        new = request.form.get("due_date", "")
        if not valid_date(new):
            flash("Choisissez la nouvelle date.", "error")
            return redirect(url_for("sanitaire.index") + f"#soin-{event_id}")
        execute("UPDATE health_events SET due_date = ?, notified = '' WHERE id = ?", (new, event_id))
        notify("lot" if ev["lot_id"] else "", ev["lot_id"],
               f"📅 Suivi sanitaire — {ev['title']} ({ev['lot'] or 'tous les lots'}) reporté du {date_fr(ev['due_date'])} "
               f"au {date_fr(new)}" + (f" : « {reason} »" if reason else "."))
        flash(f"Soin reporté au {date_fr(new)}.", "success")
    elif action == "rouvrir" and can("sanitaire", MANAGE):
        with transaction() as conn:
            conn.execute("UPDATE health_events SET status = 'prevu', done_date = NULL, done_by = NULL, cost = 0, account_id = NULL, "
                         "notified = '' WHERE id = ?", (event_id,))
            conn.execute("DELETE FROM cash_movements WHERE ref_type = 'health' AND ref_id = ?", (event_id,))
        notify("lot" if ev["lot_id"] else "", ev["lot_id"], f"↩️ Suivi sanitaire — {ev['title']} ({ev['lot'] or 'tous les lots'}) "
               "remis « à faire » (confirmation annulée).")
        flash("Le soin est de nouveau « à faire ».", "success")
    return redirect(url_for("sanitaire.index") + f"#soin-{event_id}")


@bp.route("/soin/<int:event_id>/supprimer", methods=["POST"])
@require("sanitaire", MANAGE)
def event_delete(event_id):
    ev = _event(event_id)
    with transaction() as conn:
        conn.execute("DELETE FROM cash_movements WHERE ref_type = 'health' AND ref_id = ?", (event_id,))
        conn.execute("DELETE FROM health_events WHERE id = ?", (event_id,))
    log_activity("Soin supprimé", ev["title"])
    flash("Soin supprimé (erreur de saisie).", "success")
    return redirect(url_for("sanitaire.index"))


# --- Programmes ---------------------------------------------------------------
@bp.route("/programmes")
@require("sanitaire", VIEW)
def programs():
    rows = query("""SELECT p.*, (SELECT COUNT(*) FROM health_program_items i WHERE i.program_id = p.id) AS nb,
                           (SELECT COUNT(*) FROM lots l WHERE l.health_program_id = p.id AND l.status = 'actif') AS lots
                    FROM health_programs p ORDER BY p.active DESC, p.name""")
    return render_template("sanitaire/programs.html", rows=rows)


def _program_form():
    f = request.form
    values = {"name": " ".join(f.get("name", "").split())[:80], "notes": f.get("notes", "").strip()[:500]}
    rows, parsed, errors = [], [], []
    for i, (day, title, kind, method, product) in enumerate(zip(f.getlist("day_age"), f.getlist("title"), f.getlist("kind"),
                                                                 f.getlist("method"), f.getlist("product")), 1):
        rows.append({"day_age": day, "title": title, "kind": kind, "method": method, "product": product})
        if not (day.strip() or title.strip()):
            continue
        d = parse_num(day)
        if d is None or d < 1 or int(d) != d:
            errors.append(f"Ligne {i} : le jour d'âge doit être un nombre entier (1 = jour d'arrivée).")
            continue
        if len(title.strip()) < 2:
            errors.append(f"Ligne {i} : écrivez ce qu'il faut faire (ex. Vaccin Newcastle).")
            continue
        parsed.append((int(d), " ".join(title.split())[:120], kind if kind in KINDS else "autre",
                       " ".join(method.split())[:80], " ".join(product.split())[:80]))
    if len(values["name"]) < 2:
        errors.append("Donnez un nom au programme.")
    if not parsed:
        errors.append("Ajoutez au moins une étape (jour + quoi faire).")
    return values, rows, sorted(parsed), errors


def _save_items(conn, program_id, parsed):
    # on garde les étapes déjà existantes (même jour + même nom) pour ne pas perdre ce qui a été confirmé
    old = {(r["day_age"], r["title"]): r["id"] for r in conn.execute(
        "SELECT * FROM health_program_items WHERE program_id = ?", (program_id,)).fetchall()}
    keep = set()
    for day, title, kind, method, product in parsed:
        if (day, title) in old:
            keep.add(old[(day, title)])
            conn.execute("UPDATE health_program_items SET kind = ?, method = ?, product = ? WHERE id = ?",
                         (kind, method, product, old[(day, title)]))
        else:
            conn.execute("INSERT INTO health_program_items (program_id, day_age, title, kind, method, product) VALUES (?, ?, ?, ?, ?, ?)",
                         (program_id, day, title, kind, method, product))
    for key, item_id in old.items():
        if item_id not in keep:
            conn.execute("DELETE FROM health_program_items WHERE id = ?", (item_id,))
            conn.execute("DELETE FROM health_events WHERE item_id = ? AND status = 'prevu'", (item_id,))


@bp.route("/programmes/nouveau", methods=["GET", "POST"])
@require("sanitaire", EDIT)
def program_new():
    values = {"name": "", "notes": ""}
    rows = [{"day_age": "", "title": "", "kind": "vaccin", "method": "", "product": ""}]
    if request.args.get("exemple"):
        values = {"name": "Programme pondeuses (exemple)",
                  "notes": "Exemple à adapter avec votre vétérinaire et selon les maladies de votre région."}
        rows = [{"day_age": str(d), "title": t, "kind": k, "method": m, "product": ""} for d, t, k, m in EXAMPLE]
    if request.method == "POST":
        values, rows, parsed, errors = _program_form()
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with transaction() as conn:
                cur = conn.execute("INSERT INTO health_programs (name, notes, created_at) VALUES (?, ?, ?)",
                                   (values["name"], values["notes"], now_utc()))
                _save_items(conn, cur.lastrowid, parsed)
                pid = cur.lastrowid
            log_activity("Programme sanitaire créé", values["name"])
            flash("Programme enregistré. Choisissez-le dans chaque lot (Lot → Modifier) pour créer les alertes.", "success")
            return redirect(url_for("sanitaire.program", program_id=pid))
    return render_template("sanitaire/program_form.html", values=values, rows=rows, program=None, kinds=KINDS, methods=METHODS)


@bp.route("/programmes/<int:program_id>")
@require("sanitaire", VIEW)
def program(program_id):
    item = query("SELECT * FROM health_programs WHERE id = ?", (program_id,), one=True)
    if item is None:
        abort(404)
    lots = query("SELECT * FROM lots WHERE health_program_id = ? ORDER BY arrival_date", (program_id,))
    all_lots = query("SELECT * FROM lots WHERE status = 'actif' AND (health_program_id IS NULL OR health_program_id != ?) "
                     "ORDER BY arrival_date", (program_id,))
    return render_template("sanitaire/program.html", item=item, items=program_items(program_id), lots=lots, all_lots=all_lots,
                           kinds=KINDS, describe_day=describe_day)


@bp.route("/programmes/<int:program_id>/modifier", methods=["GET", "POST"])
@require("sanitaire", EDIT)
def program_edit(program_id):
    item = query("SELECT * FROM health_programs WHERE id = ?", (program_id,), one=True)
    if item is None:
        abort(404)
    values = dict(item)
    rows = [{"day_age": str(r["day_age"]), "title": r["title"], "kind": r["kind"], "method": r["method"] or "",
             "product": r["product"] or ""} for r in program_items(program_id)]
    if request.method == "POST":
        values, rows, parsed, errors = _program_form()
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with transaction() as conn:
                conn.execute("UPDATE health_programs SET name = ?, notes = ? WHERE id = ?", (values["name"], values["notes"], program_id))
                _save_items(conn, program_id, parsed)
            reschedule_program(program_id)
            log_activity("Programme sanitaire modifié", values["name"])
            flash("Programme mis à jour : les soins à venir des lots qui le suivent ont été recalculés.", "success")
            return redirect(url_for("sanitaire.program", program_id=program_id))
    return render_template("sanitaire/program_form.html", values=values, rows=rows, program=item, kinds=KINDS, methods=METHODS)


@bp.route("/programmes/<int:program_id>/lots", methods=["POST"])
@require("sanitaire", EDIT)
def program_assign(program_id):
    item = query("SELECT * FROM health_programs WHERE id = ?", (program_id,), one=True)
    if item is None:
        abort(404)
    ids = [int(x) for x in request.form.getlist("lot_id") if x.isdigit()]
    names = []
    for lid in ids:
        lot = query("SELECT * FROM lots WHERE id = ?", (lid,), one=True)
        if lot:
            execute("UPDATE lots SET health_program_id = ? WHERE id = ?", (program_id, lid))
            schedule_lot_id(lid)
            names.append(lot["name"])
    if names:
        notify("", None, f"🩺 Programme sanitaire « {item['name']} » appliqué à : {', '.join(names)}. Les alertes arriveront 7 jours avant chaque soin.")
        flash(f"Programme appliqué à {', '.join(names)}. Les soins sont prévus avec leurs dates.", "success")
    return redirect(url_for("sanitaire.program", program_id=program_id))


@bp.route("/programmes/<int:program_id>/archiver", methods=["POST"])
@require("sanitaire", MANAGE)
def program_archive(program_id):
    item = query("SELECT * FROM health_programs WHERE id = ?", (program_id,), one=True)
    if item is None:
        abort(404)
    execute("UPDATE health_programs SET active = ? WHERE id = ?", (0 if item["active"] else 1, program_id))
    flash("Programme " + ("archivé (les lots le gardent)." if item["active"] else "réactivé."), "success")
    return redirect(url_for("sanitaire.program", program_id=program_id))


__all__ = ["bp", "alerts", "send_reminders", "schedule_lot_id", "lot_health_cost", "can_correct"]
