"""Tests automatiques de la base du logiciel.

Lancer : python -m unittest discover tests
"""
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FERME_DATA_DIR"] = tempfile.mkdtemp(prefix="ferme-test-")

from app import create_app  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
       b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferme-test-")
        os.environ["FERME_DATA_DIR"] = self.dir
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test"})
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- outils --
    def csrf(self, client=None):
        client = client or self.client
        page = client.get("/connexion").get_data(as_text=True)
        match = re.search(r'name="_csrf" value="([^"]+)"', page)
        if match:
            return match.group(1)
        with client.session_transaction() as sess:
            return sess.get("_csrf")

    def post(self, url, data=None, client=None, **kw):
        client = client or self.client
        data = dict(data or {})
        data["_csrf"] = self.token(client)
        return client.post(url, data=data, **kw)

    def token(self, client=None):
        client = client or self.client
        with client.session_transaction() as sess:
            if "_csrf" not in sess:
                sess["_csrf"] = "tok"
            return sess["_csrf"]

    def login(self, username, password, client=None):
        return self.post("/connexion", {"username": username, "password": password}, client=client)

    def setup_super_admin(self, new_password="Poule2026!"):
        self.login("super-adm", "Ampandrana")
        res = self.post("/premiere-connexion", {
            "password": new_password, "confirm": new_password, "email": "admin@example.com",
            "question": "Quel est le nom de votre premier animal de compagnie ?", "answer": "Médor",
        })
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:3000])
        return new_password

    def db(self):
        conn = sqlite3.connect(os.path.join(self.dir, "ferme.db"))
        conn.row_factory = sqlite3.Row
        return conn


class TestAuth(Base):
    def test_login_page(self):
        res = self.client.get("/connexion")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Se connecter", res.get_data(as_text=True))

    def test_redirect_when_logged_out(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/connexion", res.headers["Location"])

    def test_csrf_required(self):
        res = self.client.post("/connexion", data={"username": "super-adm", "password": "Ampandrana"})
        self.assertEqual(res.status_code, 400)

    def test_default_super_admin_must_setup(self):
        res = self.login("super-adm", "Ampandrana")
        self.assertEqual(res.status_code, 302)
        res = self.client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/premiere-connexion", res.headers["Location"])
        page = self.client.get("/premiere-connexion").get_data(as_text=True)
        self.assertIn("Question secrète", page)

    def test_setup_rejects_weak_password(self):
        self.login("super-adm", "Ampandrana")
        res = self.post("/premiere-connexion", {"password": "abc", "confirm": "abc", "question": "Question test ?", "answer": "ok"})
        self.assertIn("au moins 8", res.get_data(as_text=True))

    def test_setup_then_dashboard(self):
        self.setup_super_admin()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Tableau de bord", page)
        self.assertIn("Utilisateurs", page)

    def test_wrong_password_and_lock(self):
        for _ in range(5):
            self.login("super-adm", "faux")
        res = self.login("super-adm", "Ampandrana")
        self.assertIn("Trop d", res.get_data(as_text=True))

    def test_forgot_with_question(self):
        self.setup_super_admin()
        self.post("/deconnexion")
        self.post("/mot-de-passe-oublie", {"username": "super-adm"})
        page = self.client.get("/mot-de-passe-oublie/choix").get_data(as_text=True)
        self.assertIn("question secrète", page)
        res = self.post("/mot-de-passe-oublie/question", {"answer": "faux", "password": "Nouveau123", "confirm": "Nouveau123"})
        self.assertIn("incorrecte", res.get_data(as_text=True))
        res = self.post("/mot-de-passe-oublie/question", {"answer": "  MEDOR ", "password": "Nouveau123", "confirm": "Nouveau123"})
        self.assertEqual(res.status_code, 302)
        res = self.login("super-adm", "Nouveau123")
        self.assertEqual(res.status_code, 302)
        self.assertNotIn("/connexion", res.headers["Location"])

    def test_profile_change_password(self):
        pw = self.setup_super_admin()
        res = self.post("/mon-profil", {"action": "password", "current": pw, "password": "Autre1234", "confirm": "Autre1234"},
                        follow_redirects=True)
        self.assertIn("Mot de passe modifié", res.get_data(as_text=True))

    def test_theme(self):
        self.setup_super_admin()
        res = self.client.post("/preferences/theme", json={"theme": "dark"}, headers={"X-CSRF-Token": self.token()})
        self.assertEqual(res.get_json()["theme"], "dark")
        self.assertIn('data-theme="dark"', self.client.get("/").get_data(as_text=True))


class TestUsers(Base):
    def create_user(self, username="rakoto", role="elevage", password="Ferme-1234"):
        return self.post("/utilisateurs/nouveau", {
            "username": username, "full_name": "Rakoto Jean", "email": "", "phone": "",
            "role": role, "password": password, "use_defaults": "1",
        })

    def test_admin_creates_user_who_completes_setup(self):
        self.setup_super_admin()
        res = self.create_user()
        self.assertEqual(res.status_code, 302)
        other = self.app.test_client()
        self.login("rakoto", "Ferme-1234", client=other)
        res = other.get("/")
        self.assertIn("/premiere-connexion", res.headers["Location"])
        res = self.post("/premiere-connexion", {"password": "Akoho-2026", "confirm": "Akoho-2026", "email": "",
                                                "question": "__autre__", "custom_question": "Nom de mon village ?",
                                                "answer": "Ampandrana"}, client=other)
        self.assertEqual(res.status_code, 302)
        page = other.get("/").get_data(as_text=True)
        self.assertIn("Lots de poules", page)          # module de son rôle
        self.assertNotIn("Paramètres", page)            # pas d'administration
        self.assertEqual(other.get("/utilisateurs/").status_code, 403)
        self.assertEqual(other.get("/module/ventes").status_code, 403)
        self.assertEqual(other.get("/lots/").status_code, 200)
        self.assertEqual(other.get("/oeufs/").status_code, 200)

    def test_custom_permissions(self):
        self.setup_super_admin()
        self.create_user()
        uid = self.db().execute("SELECT id FROM users WHERE username='rakoto'").fetchone()["id"]
        data = {"full_name": "Rakoto", "email": "", "phone": "", "role": "elevage"}
        data.update({f"perm_{k}": "0" for k in ("dashboard", "lots", "alimentation", "sanitaire", "oeufs", "reformes", "provenderie")})
        data["perm_ventes"] = "2"
        self.post(f"/utilisateurs/{uid}", data)
        other = self.app.test_client()
        self.login("rakoto", "Ferme-1234", client=other)
        self.post("/premiere-connexion", {"password": "Akoho-2026", "confirm": "Akoho-2026", "question": "Question numéro un ?", "answer": "oui"}, client=other)
        self.assertEqual(other.get("/module/ventes").status_code, 200)
        self.assertEqual(other.get("/lots/").status_code, 403)
        self.assertEqual(other.get("/oeufs/").status_code, 403)

    def test_delete_rules(self):
        self.setup_super_admin()
        self.create_user()
        self.create_user("proprio", role="admin")
        conn = self.db()
        uid = conn.execute("SELECT id FROM users WHERE username='rakoto'").fetchone()["id"]
        sid = conn.execute("SELECT id FROM users WHERE role='super_admin'").fetchone()["id"]
        aid = conn.execute("SELECT id FROM users WHERE username='proprio'").fetchone()["id"]
        # Le Super-admin ne peut pas être supprimé
        self.post(f"/utilisateurs/{sid}/supprimer", {"confirm_name": "super-adm"})
        self.assertEqual(self.db().execute("SELECT deleted FROM users WHERE id=?", (sid,)).fetchone()[0], 0)
        # Le dernier Admin ne peut pas être supprimé
        self.post(f"/utilisateurs/{aid}/supprimer", {"confirm_name": "proprio"})
        self.assertEqual(self.db().execute("SELECT deleted FROM users WHERE id=?", (aid,)).fetchone()[0], 0)
        # Mauvaise confirmation : rien
        self.post(f"/utilisateurs/{uid}/supprimer", {"confirm_name": "autre"})
        self.assertEqual(self.db().execute("SELECT deleted FROM users WHERE id=?", (uid,)).fetchone()[0], 0)
        # Utilisateur normal : supprimé, et le nom redevient libre
        self.post(f"/utilisateurs/{uid}/supprimer", {"confirm_name": "rakoto"})
        self.assertEqual(self.db().execute("SELECT deleted FROM users WHERE id=?", (uid,)).fetchone()[0], 1)
        self.assertEqual(self.create_user().status_code, 302)

    def test_admin_cannot_delete_admin(self):
        self.setup_super_admin()
        self.create_user("proprio1", role="admin")
        self.create_user("proprio2", role="admin")
        admin = self.app.test_client()
        self.login("proprio1", "Ferme-1234", client=admin)
        self.post("/premiere-connexion", {"password": "Akoho-2026", "confirm": "Akoho-2026", "question": "Question numéro un ?", "answer": "oui"}, client=admin)
        aid = self.db().execute("SELECT id FROM users WHERE username='proprio2'").fetchone()["id"]
        self.post(f"/utilisateurs/{aid}/supprimer", {"confirm_name": "proprio2"}, client=admin)
        self.assertEqual(self.db().execute("SELECT deleted FROM users WHERE id=?", (aid,)).fetchone()[0], 0)
        # mais il peut supprimer un utilisateur ordinaire
        self.create_user("vendeur", role="ventes")
        vid = self.db().execute("SELECT id FROM users WHERE username='vendeur'").fetchone()["id"]
        self.post(f"/utilisateurs/{vid}/supprimer", {"confirm_name": "vendeur"}, client=admin)
        self.assertEqual(self.db().execute("SELECT deleted FROM users WHERE id=?", (vid,)).fetchone()[0], 1)
        # et il ne peut pas modifier le Super-admin
        sid = self.db().execute("SELECT id FROM users WHERE role='super_admin'").fetchone()["id"]
        self.assertEqual(admin.get(f"/utilisateurs/{sid}").status_code, 403)

    def test_deactivate_and_reset_password(self):
        self.setup_super_admin()
        self.create_user()
        uid = self.db().execute("SELECT id FROM users WHERE username='rakoto'").fetchone()["id"]
        self.post(f"/utilisateurs/{uid}/activer")
        other = self.app.test_client()
        res = self.login("rakoto", "Ferme-1234", client=other)
        self.assertIn("désactivé", res.get_data(as_text=True))
        self.post(f"/utilisateurs/{uid}/activer")
        self.post(f"/utilisateurs/{uid}/mot-de-passe", {"password": "Grain-5555"})
        res = self.login("rakoto", "Grain-5555", client=other)
        self.assertEqual(res.status_code, 302)

    def test_pages_render(self):
        self.setup_super_admin()
        self.create_user()
        uid = self.db().execute("SELECT id FROM users WHERE username='rakoto'").fetchone()["id"]
        for url in ["/", "/utilisateurs/", "/utilisateurs/?voir=tous", "/utilisateurs/nouveau", f"/utilisateurs/{uid}",
                    "/parametres/", "/parametres/?onglet=apparence", "/parametres/?onglet=email",
                    "/parametres/?onglet=securite", "/sauvegardes/", "/journal", "/journal?utilisateur=super-adm&du=2020-01-01",
                    "/mon-profil", "/oeufs/", "/module/sanitaire", "/module/ventes", "/lots/", "/lots/alimentation"]:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, url)
        self.assertEqual(self.client.get("/module/inconnu").status_code, 404)


class TestSettingsAndBackups(Base):
    def test_company_settings_and_logo(self):
        self.setup_super_admin()
        res = self.post("/parametres/", {"section": "societe", "company_name": "Ferme Ampandrana",
                                         "logo": (io.BytesIO(PNG), "logo.png")}, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 302)
        self.assertIn("Ferme Ampandrana", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get("/parametres/logo").status_code, 200)
        res = self.post("/parametres/", {"section": "societe", "company_name": "X",
                                         "logo": (io.BytesIO(b"<svg></svg>"), "logo.svg")}, content_type="multipart/form-data",
                        follow_redirects=True)
        self.assertIn("Logo refusé", res.get_data(as_text=True))

    def test_appearance(self):
        self.setup_super_admin()
        self.post("/parametres/", {"section": "apparence", "primary_color": "#ff0000", "menu_color": "#ffffff",
                                   "default_theme": "dark", "currency": "Ar"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("--primary: #ff0000", page)
        self.assertIn("--menu-fg: #14201b", page)  # texte foncé sur menu blanc

    def test_backup_restore_and_reset(self):
        self.setup_super_admin()
        self.post("/parametres/", {"section": "societe", "company_name": "Avant"})
        self.post("/sauvegardes/creer")
        backups = os.listdir(os.path.join(self.dir, "backups"))
        manual = [b for b in backups if b.startswith("manuel")]
        self.assertTrue(manual)
        self.assertTrue([b for b in backups if b.startswith("auto")])  # sauvegarde du jour
        self.post("/parametres/", {"section": "societe", "company_name": "Apres"})
        self.assertIn("Apres", self.client.get("/").get_data(as_text=True))
        res = self.post(f"/sauvegardes/restaurer/{manual[0]}", {"password": "faux"}, follow_redirects=True)
        self.assertIn("incorrect", res.get_data(as_text=True))
        self.post(f"/sauvegardes/restaurer/{manual[0]}", {"password": "Poule2026!"})
        self.assertIn("Avant", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/sauvegardes/telecharger/{manual[0]}").status_code, 200)
        self.assertEqual(self.client.get("/sauvegardes/telecharger/../ferme.db").status_code, 404)
        # Import d'un fichier
        with open(os.path.join(self.dir, "backups", manual[0]), "rb") as fh:
            data = fh.read()
        res = self.post("/sauvegardes/importer", {"fichier": (io.BytesIO(data), "copie.db"), "password": "Poule2026!"},
                        content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("restaurées depuis le fichier", res.get_data(as_text=True))
        res = self.post("/sauvegardes/importer", {"fichier": (io.BytesIO(b"pas une base"), "x.db"), "password": "Poule2026!"},
                        content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("pas une sauvegarde valide", res.get_data(as_text=True))
        # Réinitialisation
        self.post("/utilisateurs/nouveau", {"username": "rakoto", "role": "ventes", "password": "Ferme-1234", "use_defaults": "1"})
        res = self.post("/sauvegardes/reinitialiser", {"confirm_word": "effacer", "password": "Poule2026!"}, follow_redirects=True)
        self.assertIn("EFFACER", res.get_data(as_text=True))
        res = self.post("/sauvegardes/reinitialiser", {"confirm_word": "EFFACER", "password": "Poule2026!"}, follow_redirects=True)
        page = res.get_data(as_text=True)
        self.assertIn("état d", page)
        self.assertIn("Androfia Farm", page)
        names = [r[0] for r in self.db().execute("SELECT username FROM users")]
        self.assertEqual(names, ["super-adm"])
        # toujours connecté avec le même mot de passe
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_restricted_user_cannot_backup(self):
        self.setup_super_admin()
        self.post("/utilisateurs/nouveau", {"username": "rakoto", "role": "ventes", "password": "Ferme-1234", "use_defaults": "1"})
        other = self.app.test_client()
        self.login("rakoto", "Ferme-1234", client=other)
        self.post("/premiere-connexion", {"password": "Akoho-2026", "confirm": "Akoho-2026", "question": "Question numéro un ?", "answer": "oui"}, client=other)
        self.assertEqual(other.get("/sauvegardes/").status_code, 403)
        self.assertEqual(other.get("/parametres/").status_code, 403)
        self.assertEqual(other.get("/journal").status_code, 403)


class TestMigration(Base):
    def test_new_column_added_automatically(self):
        from app import db as dbmod
        dbmod.SCHEMA["users"].append(("test_extra", "TEXT DEFAULT ''"))
        try:
            create_app({"TESTING": True, "SECRET_KEY": "test"})
            cols = [r[1] for r in self.db().execute("PRAGMA table_info(users)")]
            self.assertIn("test_extra", cols)
        finally:
            dbmod.SCHEMA["users"].pop()


if __name__ == "__main__":
    unittest.main()
