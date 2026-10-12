"""Construire (ou reconstruire) la démonstration pour la vitrine.

Dans la console PythonAnywhere :
    cd ~/ferme-pondeuses && python3 demo_build.py
puis cliquer sur « Reload » dans l'onglet Web.
Les vraies données de la ferme ne sont pas touchées : la démo a sa propre base.
"""
from app import create_app
from app.demo import build_snapshot

if __name__ == "__main__":
    app = create_app()
    print("Construction de la démonstration (6 mois de données d'exemple)… patientez une minute.")
    summary = build_snapshot(app)
    print("✅ Démonstration prête :", summary)
    print("Lien à partager : https://fermeaorn.pythonanywhere.com/demo/  (adaptez si votre adresse est différente)")
