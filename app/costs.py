"""Le moteur des coûts : qui paie quoi, et quelle part revient à chaque lot.

Règle simple, la même partout dans le logiciel :
- Une dépense marquée pour un lot (« Lot A ») est entièrement à ce lot.
- Une dépense commune (électricité, salaires, usure du matériel, soin de toute la ferme…)
  est partagée entre les lots présents à la ferme ce jour-là,
  selon leur nombre de poules au départ (un lot de 1 000 poules paie 2 fois plus qu'un lot de 500).
- S'il n'y a aucun lot à la ferme ce jour-là, la dépense reste « non répartie » (elle compte
  quand même dans le résultat de la ferme).

L'usure du matériel (amortissement) : un bien de 1 200 000 Ar qui dure 5 ans coûte
1 200 000 ÷ (5 × 365) ≈ 658 Ar par jour, chaque jour, pendant 5 ans.
"""
from datetime import date as Date, timedelta

from flask import g

from .db import query
from .stock import today

KIND_LABELS = {"depense": "Dépenses", "salaire": "Salaires", "usure": "Usure du matériel", "soin": "Soins"}


def _d(value):
    return Date.fromisoformat(value)


# ---------------------------------------------------------------------------
# Amortissement
# ---------------------------------------------------------------------------
def asset_daily(asset):
    days = max(1.0, (asset["life_years"] or 1) * 365)
    return max(0.0, (asset["cost"] or 0) - (asset["residual"] or 0)) / days


def asset_end(asset):
    """Dernier jour d'usure : fin de la durée de vie, ou le jour où le bien est sorti (vendu, cassé)."""
    end = _d(asset["purchase_date"]) + timedelta(days=int(round((asset["life_years"] or 1) * 365)) - 1)
    if asset["status"] != "actif" and asset["end_date"]:
        end = min(end, _d(asset["end_date"]))
    return end


def asset_state(asset, on=None):
    """Valeur aujourd'hui, usure déjà comptée, usure par mois."""
    on = _d(on or today())
    start = _d(asset["purchase_date"])
    end = asset_end(asset)
    used_days = 0 if on < start else (min(on, end) - start).days + 1
    daily = asset_daily(asset)
    used = min(daily * used_days, max(0.0, (asset["cost"] or 0) - (asset["residual"] or 0)))
    full_days = max(1, int(round((asset["life_years"] or 1) * 365)))
    return {
        "daily": daily, "monthly": daily * 30.4, "yearly": daily * 365, "used": used,
        "value": (asset["cost"] or 0) - used, "end": end.isoformat(),
        "progress": min(100, used_days / full_days * 100), "used_days": used_days, "full_days": full_days,
        "finished": on > end,
    }


def _asset_months(asset, until):
    """Usure d'un bien découpée par mois : [(date du milieu de la période, montant)]."""
    start = _d(asset["purchase_date"])
    end = min(asset_end(asset), until)
    daily = asset_daily(asset)
    out = []
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = min(end, nxt - timedelta(days=1))
        n = (last - cur).days + 1
        out.append(((cur + timedelta(days=n // 2)).isoformat(), daily * n))
        cur = nxt
    return out


# ---------------------------------------------------------------------------
# Toutes les charges de la ferme (hors achats de matières, poussins et provende,
# déjà comptés ailleurs)
# ---------------------------------------------------------------------------
def charges(until=None):
    """Liste de (date, montant, genre, lot_id ou None)."""
    until = until or today()
    rows = []
    for e in query("SELECT date, amount, lot_id FROM expenses WHERE date <= ?", (until,)):
        rows.append((e["date"], e["amount"] or 0, "depense", e["lot_id"]))
    for p in query("SELECT date, amount FROM staff_payments WHERE date <= ?", (until,)):
        rows.append((p["date"], p["amount"] or 0, "salaire", None))
    for h in query("SELECT done_date AS date, cost, lot_id FROM health_events WHERE status = 'fait' AND cost > 0 AND done_date <= ?",
                   (until,)):
        rows.append((h["date"], h["cost"] or 0, "soin", h["lot_id"]))
    u = _d(until)
    for a in query("SELECT * FROM assets WHERE purchase_date <= ?", (until,)):
        for d, amount in _asset_months(a, u):
            rows.append((d, amount, "usure", a["lot_id"]))
    return rows


def _lots():
    return query("SELECT id, name, arrival_date, initial_count, status, end_date FROM lots")


def present_lots(on, lots=None):
    lots = lots if lots is not None else _lots()
    out = []
    for l in lots:
        if l["arrival_date"] > on:
            continue
        if l["status"] != "actif" and l["end_date"] and l["end_date"] < on:
            continue
        out.append(l)
    return out


def allocate(until=None):
    """Part de chaque lot dans les charges : {lot_id: {genre: montant}}, + la part non répartie.
    Calculé une seule fois par page."""
    key = until or today()
    cache = g.setdefault("_alloc", {})
    if key in cache:
        return cache[key]
    lots = _lots()
    result = {l["id"]: {k: 0.0 for k in KIND_LABELS} for l in lots}
    unallocated = {k: 0.0 for k in KIND_LABELS}
    for d, amount, kind, lot_id in charges(key):
        if lot_id and lot_id in result:
            result[lot_id][kind] += amount
            continue
        here = [l for l in present_lots(d, lots) if (l["initial_count"] or 0) > 0]
        total = sum(l["initial_count"] for l in here)
        if not total:
            unallocated[kind] += amount
            continue
        for l in here:
            result[l["id"]][kind] += amount * l["initial_count"] / total
    cache[key] = (result, unallocated)
    return result, unallocated


def lot_costs(lot):
    """Tout ce que coûte un lot depuis son arrivée, poste par poste."""
    from .lots import lot_summary

    s, _cons = lot_summary(lot)
    shares, _ = allocate()
    extra = shares.get(lot["id"], {k: 0.0 for k in KIND_LABELS})
    out = {"chicks": s["chick_cost"], "feed": s["eaten_cost"], "health": extra["soin"], "expenses": extra["depense"],
           "salaries": extra["salaire"], "wear": extra["usure"]}
    out["total"] = sum(out.values())
    return out


def by_month(keys):
    """Charges par mois (dépenses, salaires, usure, soins) pour les rapports."""
    out = {k: {kind: 0.0 for kind in KIND_LABELS} for k in keys}
    for d, amount, kind, _lot in charges():
        k = d[:7]
        if k in out:
            out[k][kind] += amount
    return out


__all__ = ["allocate", "lot_costs", "by_month", "asset_state", "charges", "present_lots", "KIND_LABELS"]
