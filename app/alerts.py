"""Alertes affichées dans la cloche, selon les modules de chaque utilisateur."""
from flask import g, url_for

from .security import can
from .utils import fmt_qty


def current_alerts():
    if "alerts" in g:
        return g.alerts
    items = []
    user = g.get("user")
    if user is None:
        g.alerts = items
        return items
    from .stock import formulas_overview, materials_overview

    if can("matieres"):
        for m in materials_overview():
            if m["status"] == "empty" and (m["alert_threshold"] or m["used_30"]):
                items.append({"level": "critical", "title": f"{m['name']} : stock épuisé",
                              "text": "Prévoir un achat.", "url": url_for("matieres.material", material_id=m["id"])})
            elif m["status"] == "low":
                days = f" (environ {int(m['autonomy_days'])} jour(s))" if m["autonomy_days"] is not None else ""
                items.append({"level": "warning", "title": f"{m['name']} : stock bas",
                              "text": f"Reste {fmt_qty(m['stock'], m['unit'])}{days}, seuil {fmt_qty(m['alert_threshold'], m['unit'])}.",
                              "url": url_for("matieres.material", material_id=m["id"])})
    if can("provenderie"):
        for f in formulas_overview():
            if f["status"] == "empty" and (f["alert_threshold"] or f["used_30"]):
                items.append({"level": "critical", "title": f"Provende « {f['name']} » épuisée",
                              "text": "Prévoir une fabrication.", "url": url_for("provenderie.index")})
            elif f["status"] == "low":
                items.append({"level": "warning", "title": f"Provende « {f['name']} » : stock bas",
                              "text": f"Reste {fmt_qty(f['stock'], 'kg')}, seuil {fmt_qty(f['alert_threshold'], 'kg')}.",
                              "url": url_for("provenderie.index")})
    if can("sanitaire") or can("lots"):
        from .health import alerts as health_alerts, send_reminders

        try:
            send_reminders()
            items = health_alerts() + items
        except Exception:  # pendant une mise à jour de la base
            pass
    for module, path in (("depenses", "expenses"), ("caisse", "cash"), ("personnel", "staff"), ("reformes", "reforms")):
        if can(module):
            try:
                import importlib

                mod = importlib.import_module(f".{path}", __package__)
                items.extend(mod.alerts())
            except Exception:  # une alerte ratée ne doit jamais bloquer une page
                pass
    items.sort(key=lambda a: 0 if a["level"] == "critical" else 1)
    g.alerts = items
    return items
