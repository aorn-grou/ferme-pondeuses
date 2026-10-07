"""Tests de la discussion entre associés et des commentaires."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_stock import StockBase  # noqa: E402


class TestDiscussion(StockBase):
    def partner(self, username="associe", role="admin"):
        self.post("/utilisateurs/nouveau", {"username": username, "role": role, "password": "Ferme-1234", "use_defaults": "1"})
        client = self.app.test_client()
        self.login(username, "Ferme-1234", client=client)
        self.post("/premiere-connexion", {"password": "Akoho-2026", "confirm": "Akoho-2026",
                                          "question": "Question numéro un ?", "answer": "oui"}, client=client)
        return client

    def test_chat_between_partners(self):
        other = self.partner()
        self.assertEqual(self.client.get("/discussion/").status_code, 200)
        self.post("/discussion/envoyer", {"body": "Bonjour, j'ai acheté 500 kg de maïs."})
        # l'associé voit 1 non-lu
        self.assertEqual(other.get("/discussion/compteur").get_json()["unread"], 1)
        page = other.get("/discussion/").get_data(as_text=True)
        self.assertIn("500 kg de maïs", page)
        self.assertEqual(other.get("/discussion/compteur").get_json()["unread"], 0)
        # réponse en direct (fetch)
        res = self.post("/discussion/envoyer", {"body": "Merci !"}, client=other, headers={"X-Requested-With": "fetch"})
        self.assertTrue(res.get_json()["ok"])
        first = self.one("SELECT MIN(id) FROM messages")[0]
        data = self.client.get(f"/discussion/nouveaux?apres={first}&vu=1").get_json()
        self.assertIn("Merci", data["html"])
        self.assertEqual(data["unread"], 0)
        # message vide refusé
        res = self.post("/discussion/envoyer", {"body": "  "}, follow_redirects=True)
        self.assertIn("Écrivez un message", res.get_data(as_text=True))

    def test_delete_own_message_only(self):
        other = self.partner("vendeur", "ventes")
        self.db().execute("UPDATE users SET perms=? WHERE username='vendeur'", ('{"discussion": 1}',)).connection.commit()
        self.post("/discussion/envoyer", {"body": "Message du patron"})
        mid = self.one("SELECT id FROM messages")["id"]
        self.assertEqual(self.post(f"/discussion/{mid}/supprimer", client=other).status_code, 403)
        self.post(f"/discussion/{mid}/supprimer")
        self.assertEqual(self.one("SELECT deleted FROM messages")[0], 1)

    def test_comments_on_purchase(self):
        mais = self.material("Maïs")
        self.post("/matieres/achats/nouveau", {"date": "2026-10-02", "material_id": [str(mais)], "quantity": ["10"],
                                               "unit_price": ["1000"], "pay_mode": "tout", "account_id": str(self.account())})
        pid = self.one("SELECT id FROM purchases")["id"]
        self.assertIn("Commentaires", self.client.get(f"/matieres/achats/{pid}").get_data(as_text=True))
        self.post("/discussion/commentaire", {"ref_type": "purchase", "ref_id": str(pid), "body": "Pourquoi ce prix ?",
                                              "next": f"/matieres/achats/{pid}"})
        self.assertIn("Pourquoi ce prix", self.client.get(f"/matieres/achats/{pid}").get_data(as_text=True))
        other = self.partner()
        feed = other.get("/discussion/").get_data(as_text=True)
        self.assertIn("Commentaire sur : Achat n°", feed)
        # un éleveur sans accès aux achats ne voit pas ce commentaire et ne peut pas commenter
        eleveur = self.partner("rakoto", "elevage")
        self.assertEqual(eleveur.get("/discussion/compteur").get_json()["unread"], 0)
        res = self.post("/discussion/commentaire", {"ref_type": "purchase", "ref_id": str(pid), "body": "x"}, client=eleveur)
        self.assertEqual(res.status_code, 403)
        # pages avec commentaires
        for url in [f"/matieres/matiere/{mais}", f"/matieres/achats/{pid}"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        # effacement des données de test : commentaires des achats supprimés
        self.post("/sauvegardes/effacer-donnees-test", {"confirm_word": "EFFACER", "password": "Poule2026!"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages WHERE ref_type='purchase'")[0], 0)

    def test_polling_does_not_keep_session_alive(self):
        with self.client.session_transaction() as sess:
            sess["last_seen"] = 0  # très ancien
        res = self.client.get("/discussion/compteur")
        self.assertEqual(res.status_code, 302)  # déconnecté après inactivité


if __name__ == "__main__":
    unittest.main()
