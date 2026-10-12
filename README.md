# ferme-pondeuses

Logiciel web de gestion d'une ferme de poules pondeuses : matières premières, provenderie,
lots de poules, alimentation, suivi sanitaire, œufs, poules de réforme, ventes, dépenses,
caisse des propriétaires, personnel et tableaux de bord.

## Ce qui est prêt (version 0.2)

- Connexion par nom d'utilisateur et mot de passe, verrouillage après 5 erreurs.
- Compte **Super-admin** créé automatiquement (`super-adm`). Son mot de passe de départ
  doit être changé **dès la première connexion**.
- Première connexion de chaque utilisateur : nouveau mot de passe, e-mail de récupération
  et question secrète (suggestion proposée automatiquement).
- Mot de passe oublié : question secrète, code par e-mail (Gmail) ou mot de passe
  provisoire donné par l'administrateur.
- Rôles : Super-admin, Administrateur, Achats, Provenderie, Élevage, Ventes, Caissier, Lecteur.
  Droits réglables module par module pour chaque utilisateur.
- Utilisateurs : création, modification, désactivation, suppression
  (sauf le Super-admin et les Administrateurs).
- Paramètres : nom, slogan, logo, coordonnées, NIF/STAT, couleurs, thème, e-mail, sécurité.
- Thème clair, sombre ou automatique ; interface adaptée au téléphone
  (menu ☰, tableaux transformés en cartes lisibles).
- Sauvegarde automatique chaque jour (30 gardées), sauvegarde manuelle, téléchargement,
  restauration, transfert par fichier, remise à l'état d'origine.
- Brouillon automatique des formulaires toutes les 15 secondes.
- Journal d'activité : qui a fait quoi et quand.

- **Achats & matières premières** : matières (catégories libres, seuils d'alerte, stock initial),
  fournisseurs et dettes, achats multi-lignes avec frais de transport répartis, paiement total,
  partiel ou à crédit depuis une caisse ou un propriétaire, pertes, inventaire physique, prix moyen pondéré.
- **Provenderie** : formules libres, programmes d'alimentation par semaine d'âge, fabrication avec
  contrôle du stock et coût de revient au kg, stock de provende, pertes et inventaire.
- Cloche d'alertes (stock bas ou épuisé), caisses et propriétaires paramétrables.
- Bouton « Effacer les données de test » (garde utilisateurs, paramètres et listes).

Les autres modules sont visibles dans le menu avec la mention « bientôt ».

## Technique

- Python 3 + Flask uniquement (déjà installé sur PythonAnywhere).
- Base de données SQLite, créée et mise à jour automatiquement.
- Les données (base, sauvegardes, logo) sont dans le dossier `instance/`,
  qui n'est **jamais** envoyé sur GitHub.

## Mise en ligne sur PythonAnywhere

Point d'entrée WSGI : `wsgi.py` (variable `application`).

## Tests

```
python -m unittest discover tests
```

## Version de démonstration (vitrine)

Une ferme d'exemple (6 mois de données) que les visiteurs peuvent essayer sans toucher aux vraies données :

```
cd ~/ferme-pondeuses && python3 demo_build.py
```

puis **Reload** dans l'onglet Web. Lien à partager : `https://<votre-site>/demo/` (aussi le bouton « Découvrir le logiciel » sur la page de connexion).
La démo a sa propre base, se remet à zéro chaque jour, et bloque les comptes, sauvegardes et paramètres.

## Guide d'utilisation

Dans le logiciel : bouton **?** en haut à droite (ouvre l'aide de la page en cours) ou menu du compte → *Guide d'utilisation*. Bouton *Imprimer le guide* pour le PDF.
