"""Rappels du suivi sanitaire, même si personne n'ouvre le logiciel.

Sur PythonAnywhere : onglet « Tasks » → tâche quotidienne (ex. 06:00) avec la commande :
    cd ~/ferme-pondeuses && python3 rappels.py
Chaque soin est signalé une fois 7 jours avant, une fois le jour J, puis une fois en retard
(l'alerte rouge reste ensuite affichée dans le logiciel jusqu'à la confirmation).
"""
from app import create_app
from app.health import pending, send_reminders

if __name__ == "__main__":
    app = create_app()
    with app.test_request_context("/"):
        rows = pending()
        send_reminders()
    print(f"✅ Rappels vérifiés : {len(rows)} soin(s) à faire dans les 7 jours ou en retard.")
