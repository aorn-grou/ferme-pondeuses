"""Tests des modules Achats & matières premières et Provenderie."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_base import Base  # noqa: E402


class StockBase(Base):
    def setUp(self):
        super().setUp()
        self.setup_super_admin()

    def one(self, sql, params=()):
        return self.db().execute(sql, params).fetchone()

    def today(self):
        from app.stock import today
        with self.app.test_request_context():
            return today()

    def material(self, name, unit="kg", qty="", cost="", threshold=""):
        self.post("/matieres/matiere/nouvelle", {"name": name, "unit": unit, "initial_qty": qty,
                                                 "initial_cost": cost, "alert_threshold": threshold})
        return self.one("SELECT id FROM materials WHERE name = ?", (name,))["id"]

    def stock(self, material_id):
        return self.one("SELECT COALESCE(SUM(quantity),0) FROM stock_moves WHERE material_id = ?", (material_id,))[0]

    def feed(self, formula_id):
        return self.one("SELECT COALESCE(SUM(quantity),0) FROM feed_moves WHERE formula_id = ?", (formula_id,))[0]

    def account(self, name="Caisse de la ferme"):
        return self.one("SELECT id FROM accounts WHERE name = ?", (name,))["id"]


class TestMatieres(StockBase):
    def test_defaults_seeded(self):
        self.assertEqual(self.one("SELECT COUNT(*) FROM accounts")[0], 4)
        self.assertGreater(self.one("SELECT COUNT(*) FROM categories")[0], 3)

    def test_material_initial_stock(self):
        mid = self.material("Maïs", qty="1 000", cost="1200")
        self.assertAlmostEqual(self.stock(mid), 1000)
        self.assertAlmostEqual(self.one("SELECT avg_cost FROM materials WHERE id=?", (mid,))[0], 1200)
        res = self.post("/matieres/matiere/nouvelle", {"name": "maïs", "unit": "kg"}, follow_redirects=True)
        self.assertIn("porte déjà ce nom", res.get_data(as_text=True))

    def test_purchase_flow(self):
        mais = self.material("Maïs", qty="100", cost="1000")
        soja = self.material("Soja")
        self.post("/matieres/fournisseurs/nouveau", {"name": "Rabe Grains", "phone": "034"})
        sup = self.one("SELECT id FROM suppliers")["id"]
        # crédit sans fournisseur refusé
        res = self.post("/matieres/achats/nouveau", {
            "date": "2026-10-01", "supplier_id": "", "material_id": [str(mais)], "quantity": ["100"],
            "unit_price": ["1400"], "pay_mode": "credit"}, follow_redirects=True)
        self.assertIn("doit avoir un fournisseur", res.get_data(as_text=True))
        # achat 2 lignes + transport, payé en partie
        res = self.post("/matieres/achats/nouveau", {
            "date": "2026-10-02", "supplier_id": str(sup), "reference": "F12",
            "material_id": [str(mais), str(soja), ""], "quantity": ["100", "50", ""],
            "unit_price": ["1 400", "3000", ""], "transport_cost": "29000",
            "pay_mode": "partiel", "paid": "200000", "account_id": str(self.account())})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:2000])
        purchase = self.one("SELECT * FROM purchases")
        self.assertAlmostEqual(purchase["total"], 140000 + 150000 + 29000)
        self.assertAlmostEqual(self.stock(mais), 200)
        # transport réparti : maïs 140000/290000*29000 = 14000 -> 1540/kg ; moyenne (100*1000+100*1540)/200 = 1270
        self.assertAlmostEqual(self.one("SELECT avg_cost FROM materials WHERE id=?", (mais,))[0], 1270)
        self.assertAlmostEqual(self.one("SELECT avg_cost FROM materials WHERE id=?", (soja,))[0], 3300)
        cash = self.one("SELECT SUM(amount) FROM cash_movements")[0]
        self.assertAlmostEqual(cash, -200000)
        page = self.client.get(f"/matieres/fournisseurs/{sup}").get_data(as_text=True)
        self.assertIn("119", page)  # reste 119 000
        # paiement trop grand refusé, puis paiement correct
        res = self.post(f"/matieres/fournisseurs/{sup}/reglement", {"amount": "500000", "account_id": str(self.account()),
                                                                     "date": "2026-10-03"}, follow_redirects=True)
        self.assertIn("dépasse la dette", res.get_data(as_text=True))
        self.post(f"/matieres/fournisseurs/{sup}/reglement", {"amount": "119000", "account_id": str(self.account("Propriétaire 1")),
                                                               "date": "2026-10-03"})
        self.assertAlmostEqual(self.one("SELECT SUM(amount) FROM cash_movements")[0], -319000)
        self.assertIn("À jour", self.client.get("/matieres/fournisseurs").get_data(as_text=True))
        # pages
        for url in ["/matieres/", "/matieres/achats", f"/matieres/achats/{purchase['id']}", f"/matieres/matiere/{mais}",
                    "/matieres/fournisseurs", f"/matieres/fournisseurs/{sup}", "/matieres/categories",
                    "/matieres/inventaire", "/matieres/mouvement", "/matieres/achats/nouveau", "/matieres/archives",
                    f"/matieres/matiere/{mais}/modifier", f"/matieres/fournisseurs/{sup}/modifier",
                    "/matieres/achats?du=2026-10-01&au=2026-10-31"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_purchase_validation(self):
        mais = self.material("Maïs")
        res = self.post("/matieres/achats/nouveau", {"date": "2026-10-02", "material_id": [str(mais)], "quantity": ["0"],
                                                      "unit_price": ["10"], "pay_mode": "tout",
                                                      "account_id": str(self.account())}, follow_redirects=True)
        self.assertIn("supérieure à 0", res.get_data(as_text=True))
        res = self.post("/matieres/achats/nouveau", {"date": "2026-10-02", "material_id": [str(mais)], "quantity": ["5"],
                                                      "unit_price": ["10"], "pay_mode": "tout"}, follow_redirects=True)
        self.assertIn("où vient l&#39;argent", res.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM purchases")[0], 0)

    def test_loss_inventory_and_cancel(self):
        mais = self.material("Maïs", qty="100", cost="1000")
        res = self.post("/matieres/mouvement", {"material_id": str(mais), "kind": "perte", "quantity": "500",
                                                "date": "2026-10-02"}, follow_redirects=True)
        self.assertIn("Stock insuffisant", res.get_data(as_text=True))
        self.post("/matieres/mouvement", {"material_id": str(mais), "kind": "perte", "quantity": "10", "date": "2026-10-02"})
        self.assertAlmostEqual(self.stock(mais), 90)
        self.post("/matieres/inventaire", {"date": "2026-10-03", f"count_{mais}": "85,5"})
        self.assertAlmostEqual(self.stock(mais), 85.5)
        move = self.one("SELECT id FROM stock_moves WHERE kind='inventaire'")["id"]
        self.post(f"/matieres/mouvement/{move}/supprimer")
        self.assertAlmostEqual(self.stock(mais), 90)
        self.assertAlmostEqual(self.one("SELECT avg_cost FROM materials WHERE id=?", (mais,))[0], 1000)

    def test_categories_and_archive(self):
        self.post("/matieres/categories", {"action": "add", "name": "Coquillages"})
        cat = self.one("SELECT id FROM categories WHERE name='Coquillages'")["id"]
        self.post("/matieres/matiere/nouvelle", {"name": "Coquillage", "category_id": str(cat), "unit": "kg"})
        res = self.post("/matieres/categories", {"action": "delete", "id": str(cat)}, follow_redirects=True)
        self.assertIn("Impossible", res.get_data(as_text=True))
        mid = self.one("SELECT id FROM materials WHERE name='Coquillage'")["id"]
        self.post(f"/matieres/matiere/{mid}/archiver", {"action": "delete"})
        self.assertIsNone(self.one("SELECT id FROM materials WHERE id=?", (mid,)))
        self.post("/matieres/categories", {"action": "delete", "id": str(cat)})
        self.assertIsNone(self.one("SELECT id FROM categories WHERE id=?", (cat,)))

    def test_alert_low_stock(self):
        self.material("Prémix", qty="5", cost="9000", threshold="10")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Prémix : stock bas", page)
        self.assertIn("bell-count", page)


class TestProvenderie(StockBase):
    def setup_formula(self):
        self.mais = self.material("Maïs", qty="1000", cost="1000")
        self.son = self.material("Son", qty="300", cost="500")
        self.soja = self.material("Soja", qty="50", cost="3000")
        res = self.post("/provenderie/formules/nouvelle", {
            "name": "Ponte 1", "phase": "Ponte", "base_qty": "", "alert_threshold": "100",
            "material_id": [str(self.mais), str(self.son), str(self.soja)], "quantity": ["60", "25", "15"]})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:2000])
        self.formula = self.one("SELECT * FROM formulas")
        return self.formula["id"]

    def test_formula_and_production(self):
        fid = self.setup_formula()
        self.assertAlmostEqual(self.formula["base_qty"], 100)
        # 500 kg demande 75 kg de soja : il n'y en a que 50
        res = self.post("/provenderie/fabrication/nouvelle", {"formula_id": str(fid), "quantity": "500", "date": "2026-10-04"},
                        follow_redirects=True)
        self.assertIn("Stock insuffisant de « Soja »", res.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM productions")[0], 0)
        self.assertAlmostEqual(self.stock(self.mais), 1000)
        # 200 kg : maïs 120, son 50, soja 30
        res = self.post("/provenderie/fabrication/nouvelle", {"formula_id": str(fid), "quantity": "200", "date": "2026-10-04"})
        self.assertEqual(res.status_code, 302)
        self.assertAlmostEqual(self.stock(self.mais), 880)
        self.assertAlmostEqual(self.stock(self.soja), 20)
        self.assertAlmostEqual(self.feed(fid), 200)
        prod = self.one("SELECT * FROM productions")
        expected = 120 * 1000 + 50 * 500 + 30 * 3000
        self.assertAlmostEqual(prod["cost_total"], expected)
        self.assertAlmostEqual(prod["cost_per_kg"], expected / 200)
        self.assertAlmostEqual(self.one("SELECT avg_cost FROM formulas WHERE id=?", (fid,))[0], expected / 200)
        # achat annulé impossible : on ne peut pas retirer le stock initial utilisé ? (stock initial = mouvement)
        move = self.one("SELECT id FROM stock_moves WHERE material_id=? AND kind='stock_initial'", (self.soja,))["id"]
        res = self.post(f"/matieres/mouvement/{move}/supprimer", follow_redirects=True)
        self.assertIn("déjà été utilisée", res.get_data(as_text=True))
        # pages
        for url in ["/provenderie/", "/provenderie/formules", f"/provenderie/formules/{fid}", f"/provenderie/formules/{fid}/modifier",
                    "/provenderie/formules/nouvelle", f"/provenderie/formules/nouvelle?copie={fid}", "/provenderie/fabrications",
                    f"/provenderie/fabrications/{prod['id']}", "/provenderie/fabrication/nouvelle", f"/provenderie/stock/{fid}",
                    "/provenderie/mouvement", "/provenderie/programmes", "/provenderie/programmes/nouveau",
                    f"/matieres/matiere/{self.mais}"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        # annulation de la fabrication
        self.post(f"/provenderie/fabrications/{prod['id']}/supprimer")
        self.assertAlmostEqual(self.stock(self.soja), 50)
        self.assertAlmostEqual(self.feed(fid), 0)

    def test_production_cancel_blocked_after_loss(self):
        fid = self.setup_formula()
        self.post("/provenderie/fabrication/nouvelle", {"formula_id": str(fid), "quantity": "100", "date": "2026-10-04"})
        self.post("/provenderie/mouvement", {"formula_id": str(fid), "kind": "perte", "quantity": "10", "date": "2026-10-05"})
        self.assertAlmostEqual(self.feed(fid), 90)
        prod = self.one("SELECT id FROM productions")["id"]
        res = self.post(f"/provenderie/fabrications/{prod}/supprimer", follow_redirects=True)
        self.assertIn("déjà été distribuée", res.get_data(as_text=True))
        self.post("/provenderie/mouvement", {"formula_id": str(fid), "kind": "inventaire", "quantity": "88", "date": "2026-10-06"})
        self.assertAlmostEqual(self.feed(fid), 88)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Ponte 1", page)  # alerte : 88 kg < seuil 100 kg

    def test_formula_validation(self):
        mais = self.material("Maïs")
        res = self.post("/provenderie/formules/nouvelle", {"name": "X", "material_id": [str(mais), str(mais)],
                                                           "quantity": ["10", "5"]}, follow_redirects=True)
        page = res.get_data(as_text=True)
        self.assertIn("apparaît deux fois", page)
        self.assertIn("Donnez un nom", page)

    def test_programs(self):
        fid = self.setup_formula()
        res = self.post("/provenderie/programmes/nouveau", {"name": "Standard", "week_from": ["1", "4"],
                                                            "week_to": ["5", "8"], "formula_id": [str(fid), str(fid)],
                                                            "grams_per_bird": ["", ""]}, follow_redirects=True)
        self.assertIn("se chevauchent", res.get_data(as_text=True))
        res = self.post("/provenderie/programmes/nouveau", {"name": "Standard", "week_from": ["1", "2", "3"],
                                                            "week_to": ["1", "2", "20"], "formula_id": [str(fid)] * 3,
                                                            "grams_per_bird": ["10", "15", "110"]})
        self.assertEqual(res.status_code, 302)
        pid = self.one("SELECT id FROM feed_programs")["id"]
        self.assertEqual(self.one("SELECT COUNT(*) FROM feed_program_weeks WHERE program_id=?", (pid,))[0], 3)
        for url in [f"/provenderie/programmes/{pid}", f"/provenderie/programmes/{pid}/modifier",
                    f"/provenderie/programmes/nouveau?copie={pid}", "/provenderie/programmes"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        # une formule utilisée dans un programme actif ne peut pas être archivée
        res = self.post(f"/provenderie/formules/{fid}/archiver", follow_redirects=True)
        self.assertIn("programme actif", res.get_data(as_text=True))


class TestPermissionsAndReset(StockBase):
    def make_user(self, username, role):
        self.post("/utilisateurs/nouveau", {"username": username, "role": role, "password": "Ferme-1234", "use_defaults": "1"})
        client = self.app.test_client()
        self.login(username, "Ferme-1234", client=client)
        self.post("/premiere-connexion", {"password": "Akoho-2026", "confirm": "Akoho-2026",
                                          "question": "Question numéro un ?", "answer": "oui"}, client=client)
        return client

    def test_role_access(self):
        eleveur = self.make_user("rakoto", "elevage")
        self.assertEqual(eleveur.get("/provenderie/").status_code, 200)            # voir
        self.assertEqual(eleveur.get("/provenderie/fabrication/nouvelle").status_code, 403)  # pas fabriquer
        self.assertEqual(eleveur.get("/matieres/").status_code, 403)
        acheteur = self.make_user("vola", "achats")
        self.assertEqual(acheteur.get("/matieres/achats/nouveau").status_code, 200)
        self.assertEqual(acheteur.get("/provenderie/formules/nouvelle").status_code, 403)
        fab = self.make_user("koto", "provenderie")
        self.assertEqual(fab.get("/provenderie/fabrication/nouvelle").status_code, 200)
        self.assertEqual(fab.get("/matieres/achats/nouveau").status_code, 403)

    def test_clear_test_data(self):
        mais = self.material("Maïs", qty="100", cost="1000")
        self.post("/matieres/achats/nouveau", {"date": "2026-10-02", "material_id": [str(mais)], "quantity": ["10"],
                                               "unit_price": ["1000"], "pay_mode": "tout", "account_id": str(self.account())})
        res = self.post("/sauvegardes/effacer-donnees-test", {"confirm_word": "EFFACER", "password": "faux"},
                        follow_redirects=True)
        self.assertIn("incorrect", res.get_data(as_text=True))
        self.post("/sauvegardes/effacer-donnees-test", {"confirm_word": "EFFACER", "password": "Poule2026!"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM purchases")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM stock_moves")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM cash_movements")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM materials")[0], 1)          # liste gardée
        self.assertEqual(self.one("SELECT COUNT(*) FROM users")[0], 1)
        self.post("/sauvegardes/effacer-donnees-test", {"confirm_word": "EFFACER", "password": "Poule2026!", "clear_lists": "1"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM materials")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM accounts")[0], 4)          # caisses gardées

    def test_accounts_settings(self):
        self.post("/parametres/", {"section": "comptes", "action": "add", "name": "Banque BOA", "kind": "caisse"})
        acc = self.one("SELECT id FROM accounts WHERE name='Banque BOA'")["id"]
        p1 = self.account("Propriétaire 1")
        self.post("/parametres/", {"section": "comptes", "action": "rename", "id": str(p1), "name": "Rakoto (associé)",
                                   "kind": "proprietaire"})
        self.assertIsNotNone(self.one("SELECT id FROM accounts WHERE name='Rakoto (associé)'"))
        self.post("/parametres/", {"section": "comptes", "action": "toggle", "id": str(acc)})
        self.assertEqual(self.one("SELECT active FROM accounts WHERE id=?", (acc,))[0], 0)
        self.assertEqual(self.client.get("/parametres/?onglet=comptes").status_code, 200)

    def test_factory_reset_reseeds(self):
        self.material("Maïs")
        self.post("/sauvegardes/reinitialiser", {"confirm_word": "EFFACER", "password": "Poule2026!"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM materials")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM accounts")[0], 4)


if __name__ == "__main__":
    unittest.main()


class TestCorrections(StockBase):
    def test_correct_initial_stock(self):
        mais = self.material("Maïs", qty="8000", cost="1200")
        move = self.one("SELECT id FROM stock_moves WHERE material_id=?", (mais,))["id"]
        page = self.client.get(f"/matieres/matiere/{mais}").get_data(as_text=True)
        self.assertIn("Corriger", page)
        self.post(f"/matieres/mouvement/{move}/corriger", {"quantity": "800", "unit_cost": "1250", "date": "2026-10-07"})
        self.assertAlmostEqual(self.stock(mais), 800)
        self.assertAlmostEqual(self.one("SELECT avg_cost FROM materials WHERE id=?", (mais,))[0], 1250)
        log = self.one("SELECT details FROM activity_log WHERE action='Mouvement de stock corrigé'")["details"]
        self.assertIn("8", log)
        self.assertIn("→", log)

    def test_correct_purchase(self):
        mais = self.material("Maïs")
        soja = self.material("Soja")
        self.post("/matieres/fournisseurs/nouveau", {"name": "Rabe"})
        sup = self.one("SELECT id FROM suppliers")["id"]
        acc = self.account()
        self.post("/matieres/achats/nouveau", {"date": "2026-10-02", "supplier_id": str(sup), "material_id": [str(mais)],
                                               "quantity": ["5000"], "unit_price": ["1300"], "pay_mode": "credit"})
        pid = self.one("SELECT id FROM purchases")["id"]
        self.assertEqual(self.client.get(f"/matieres/achats/{pid}/corriger").status_code, 200)
        res = self.post(f"/matieres/achats/{pid}/corriger", {
            "date": "2026-10-02", "supplier_id": str(sup), "material_id": [str(mais), str(soja)],
            "quantity": ["500", "10"], "unit_price": ["1350", "3000"], "pay_mode": "partiel", "paid": "100000",
            "account_id": str(acc)})
        self.assertEqual(res.status_code, 302)
        self.assertAlmostEqual(self.stock(mais), 500)
        self.assertAlmostEqual(self.stock(soja), 10)
        p = self.one("SELECT * FROM purchases WHERE id=?", (pid,))
        self.assertAlmostEqual(p["total"], 500 * 1350 + 30000)
        self.assertAlmostEqual(self.one("SELECT SUM(amount) FROM cash_movements")[0], -100000)
        self.assertAlmostEqual(self.one("SELECT avg_cost FROM materials WHERE id=?", (mais,))[0], 1350)
        self.assertEqual(self.one("SELECT COUNT(*) FROM purchases")[0], 1)
        log = self.one("SELECT details FROM activity_log WHERE action='Achat corrigé'")["details"]
        self.assertIn("Soja ajouté", log)

    def test_correct_purchase_blocked_when_used(self):
        mais = self.material("Maïs")
        self.post("/matieres/achats/nouveau", {"date": "2026-10-02", "material_id": [str(mais)], "quantity": ["100"],
                                               "unit_price": ["1000"], "pay_mode": "tout", "account_id": str(self.account())})
        self.post("/matieres/mouvement", {"material_id": str(mais), "kind": "perte", "quantity": "80", "date": "2026-10-03"})
        pid = self.one("SELECT id FROM purchases")["id"]
        res = self.post(f"/matieres/achats/{pid}/corriger", {"date": "2026-10-02", "material_id": [str(mais)],
                                                             "quantity": ["50"], "unit_price": ["1000"], "pay_mode": "tout",
                                                             "account_id": str(self.account())}, follow_redirects=True)
        self.assertIn("déjà été utilisé", res.get_data(as_text=True))
        self.assertAlmostEqual(self.stock(mais), 20)

    def test_correct_payment(self):
        mais = self.material("Maïs")
        self.post("/matieres/fournisseurs/nouveau", {"name": "Rabe"})
        sup = self.one("SELECT id FROM suppliers")["id"]
        self.post("/matieres/achats/nouveau", {"date": "2026-10-02", "supplier_id": str(sup), "material_id": [str(mais)],
                                               "quantity": ["100"], "unit_price": ["1000"], "pay_mode": "credit"})
        self.post(f"/matieres/fournisseurs/{sup}/reglement", {"amount": "10000", "account_id": str(self.account()), "date": "2026-10-03"})
        pay = self.one("SELECT id FROM supplier_payments")["id"]
        self.post(f"/matieres/reglements/{pay}/corriger", {"amount": "20000", "account_id": str(self.account()), "date": "2026-10-03"})
        self.assertAlmostEqual(self.one("SELECT amount FROM supplier_payments")[0], 20000)
        self.assertAlmostEqual(self.one("SELECT SUM(amount) FROM cash_movements")[0], -20000)

    def test_correct_production_and_feed_move(self):
        mais = self.material("Maïs", qty="1000", cost="1000")
        son = self.material("Son", qty="1000", cost="500")
        self.post("/provenderie/formules/nouvelle", {"name": "Ponte", "material_id": [str(mais), str(son)], "quantity": ["70", "30"]})
        fid = self.one("SELECT id FROM formulas")["id"]
        self.post("/provenderie/fabrication/nouvelle", {"formula_id": str(fid), "quantity": "1000", "date": "2026-10-04"})
        prod = self.one("SELECT id FROM productions")["id"]
        self.assertAlmostEqual(self.stock(mais), 300)
        self.post(f"/provenderie/fabrications/{prod}/corriger", {"quantity": "100", "date": "2026-10-04"})
        self.assertAlmostEqual(self.stock(mais), 930)
        self.assertAlmostEqual(self.feed(fid), 100)
        # trop grand : refusé et rien ne change
        res = self.post(f"/provenderie/fabrications/{prod}/corriger", {"quantity": "5000", "date": "2026-10-04"},
                        follow_redirects=True)
        self.assertIn("Stock insuffisant", res.get_data(as_text=True))
        self.assertAlmostEqual(self.stock(mais), 930)
        self.assertAlmostEqual(self.feed(fid), 100)
        self.post("/provenderie/mouvement", {"formula_id": str(fid), "kind": "perte", "quantity": "50", "date": "2026-10-05"})
        mv = self.one("SELECT id FROM feed_moves WHERE kind='perte'")["id"]
        self.post(f"/provenderie/stock/mouvement/{mv}/corriger", {"quantity": "5", "date": "2026-10-05"})
        self.assertAlmostEqual(self.feed(fid), 95)
        for url in [f"/provenderie/fabrications/{prod}", f"/provenderie/stock/{fid}"]:
            self.assertIn("Corriger", self.client.get(url).get_data(as_text=True))

    def test_employee_corrects_only_own_entry_of_the_day(self):
        mais = self.material("Maïs", qty="100", cost="1000")
        self.post("/utilisateurs/nouveau", {"username": "vola", "role": "achats", "password": "Ferme-1234", "use_defaults": "1"})
        uid = self.one("SELECT id FROM users WHERE username='vola'")["id"]
        self.db().execute("UPDATE users SET perms=? WHERE id=?", ('{"matieres": 2}', uid)).connection.commit()
        emp = self.app.test_client()
        self.login("vola", "Ferme-1234", client=emp)
        self.post("/premiere-connexion", {"password": "Akoho-2026", "confirm": "Akoho-2026",
                                          "question": "Question numéro un ?", "answer": "oui"}, client=emp)
        admin_move = self.one("SELECT id FROM stock_moves")["id"]
        res = self.post(f"/matieres/mouvement/{admin_move}/corriger", {"quantity": "1", "date": "2026-10-07"}, client=emp)
        self.assertEqual(res.status_code, 403)
        self.post("/matieres/mouvement", {"material_id": str(mais), "kind": "perte", "quantity": "5", "date": "2026-10-07"}, client=emp)
        own = self.one("SELECT id FROM stock_moves WHERE kind='perte'")["id"]
        res = self.post(f"/matieres/mouvement/{own}/corriger", {"quantity": "3", "date": "2026-10-07"}, client=emp)
        self.assertEqual(res.status_code, 302)
        self.assertAlmostEqual(self.stock(mais), 97)


class TestSaisieLibre(StockBase):
    def test_new_category_and_free_unit(self):
        res = self.post("/matieres/matiere/nouvelle", {"name": "Poudre de coquillage", "category_name": "Calcium",
                                                       "unit": "bidon 5 L"})
        self.assertEqual(res.status_code, 302)
        row = self.one("SELECT m.unit, c.name FROM materials m JOIN categories c ON c.id = m.category_id "
                       "WHERE m.name = 'Poudre de coquillage'")
        self.assertEqual((row[0], row[1]), ("bidon 5 L", "Calcium"))
        # même nom de catégorie (majuscules différentes) : pas de doublon
        self.post("/matieres/matiere/nouvelle", {"name": "Coquilles", "category_name": "calcium", "unit": "g"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM categories WHERE name = 'Calcium' COLLATE NOCASE")[0], 1)
        self.assertEqual(self.one("SELECT unit FROM materials WHERE name = 'Coquilles'")[0], "g")
        # catégorie laissée vide : aucune catégorie
        self.post("/matieres/matiere/nouvelle", {"name": "Sel", "category_name": "", "unit": "kg"})
        self.assertIsNone(self.one("SELECT category_id FROM materials WHERE name = 'Sel'")[0])
        # le formulaire propose des cases où l'on écrit directement
        page = self.client.get("/matieres/matiere/nouvelle").get_data(as_text=True)
        self.assertIn('name="category_name"', page)
        self.assertIn('list="liste-unites"', page)

    def test_new_supplier_in_purchase(self):
        mais = self.material("Maïs")
        res = self.post("/matieres/achats/nouveau", {
            "date": "2026-10-02", "supplier_name": "Rakoto Provende",
            "material_id": [str(mais)], "quantity": ["10"], "unit_price": ["1000"], "pay_mode": "credit"})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:1500])
        sup = self.one("SELECT id FROM suppliers WHERE name = 'Rakoto Provende'")
        self.assertIsNotNone(sup)
        self.assertEqual(self.one("SELECT supplier_id FROM purchases")[0], sup[0])
        # même nom écrit en minuscules : on réutilise le fournisseur existant
        self.post("/matieres/achats/nouveau", {
            "date": "2026-10-03", "supplier_name": "rakoto provende",
            "material_id": [str(mais)], "quantity": ["5"], "unit_price": ["1000"], "pay_mode": "credit"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM suppliers")[0], 1)
        # vide = marché, payé comptant
        res = self.post("/matieres/achats/nouveau", {
            "date": "2026-10-03", "supplier_name": "", "material_id": [str(mais)], "quantity": ["5"],
            "unit_price": ["1000"], "pay_mode": "comptant", "account_id": str(self.account())})
        self.assertEqual(res.status_code, 302)

    def test_brand_upgrade(self):
        conn = self.db()
        conn.execute("UPDATE settings SET value = 'Ma Ferme Avicole' WHERE key = 'company_name'")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('company_name', 'Ma Ferme Avicole')")
        conn.commit()
        from app.db import upgrade_brand
        upgrade_brand(conn)
        self.assertEqual(self.one("SELECT value FROM settings WHERE key = 'company_name'")[0], "Androfia Farm")


class TestSaisieLibrePartout(StockBase):
    def test_purchase_with_new_material_and_account(self):
        res = self.post("/matieres/achats/nouveau", {
            "date": "2026-10-08", "supplier_name": "Rabe Grains",
            "material_id": ["__new__:Grain de sorgho|sac", "__new__:grain de SORGHO|sac"],
            "quantity": ["10", "5"], "unit_price": ["30000", "30000"],
            "pay_mode": "tout", "account_id": "__new__:Caisse annexe"})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:1500])
        mat = self.one("SELECT * FROM materials WHERE name = 'Grain de sorgho'")
        self.assertEqual(mat["unit"], "sac")
        self.assertEqual(self.one("SELECT COUNT(*) FROM materials WHERE name = 'Grain de sorgho' COLLATE NOCASE")[0], 1)
        self.assertAlmostEqual(self.stock(mat["id"]), 15)
        acc = self.one("SELECT id FROM accounts WHERE name = 'Caisse annexe'")
        self.assertIsNotNone(acc)
        self.assertAlmostEqual(self.one("SELECT SUM(amount) FROM cash_movements WHERE account_id = ?", (acc[0],))[0], -450000)

    def test_existing_name_is_reused(self):
        mais = self.material("Maïs grain")
        self.post("/matieres/mouvement", {"kind": "stock_initial", "material_id": "__new__:maïs GRAIN|kg",
                                          "quantity": "50", "date": "2026-10-08", "unit_cost": "1000"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM materials")[0], 1)
        self.assertAlmostEqual(self.stock(mais), 50)

    def test_formula_free_entry(self):
        # provende achetée toute faite : la formule est créée
        self.post("/provenderie/mouvement", {"kind": "entree", "formula_id": "__new__:Provende du commerce",
                                             "quantity": "100", "date": "2026-10-08", "unit_cost": "2000"})
        self.assertIsNotNone(self.one("SELECT id FROM formulas WHERE name = 'Provende du commerce'"))
        # fabrication d'une formule inconnue : refusée avec explication, rien n'est créé
        res = self.post("/provenderie/fabrication/nouvelle", {"formula_id": "__new__:Formule fantôme",
                                                              "quantity": "100", "date": "2026-10-08"},
                        follow_redirects=True)
        self.assertIn("composition", res.get_data(as_text=True))
        self.assertIsNone(self.one("SELECT id FROM formulas WHERE name = 'Formule fantôme'"))

    def test_reader_cannot_create(self):
        eleveur = TestPermissionsAndReset.make_user(self, "rakoto", "elevage")
        res = self.post("/matieres/mouvement", {"kind": "stock_initial", "material_id": "__new__:Intrus|kg",
                                                "quantity": "1", "date": "2026-10-08"}, client=eleveur)
        self.assertEqual(res.status_code, 403)
        self.assertIsNone(self.one("SELECT id FROM materials WHERE name = 'Intrus'"))


class TestSignalements(StockBase):
    def test_inventory_and_loss_notify_partners(self):
        son = self.material("Son de riz", qty="400", cost="450")
        self.post("/matieres/inventaire", {"date": "2026-10-08", f"count_{son}": "399"})
        msg = self.one("SELECT * FROM messages WHERE auto = 1 AND ref_type = 'material' AND ref_id = ?", (son,))
        self.assertIsNotNone(msg)
        self.assertIn("Écart d'inventaire", msg["body"])
        self.assertIn("399", msg["body"])
        self.post("/matieres/mouvement", {"kind": "perte", "material_id": str(son), "quantity": "9",
                                          "date": "2026-10-08", "notes": "sacs mouillés"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages WHERE auto = 1")[0], 2)
        page = self.client.get("/discussion/").get_data(as_text=True)
        self.assertIn("Signalement automatique", page)
        self.assertIn("sacs mouillés", page)


class TestUnitesFormule(StockBase):
    def test_formula_in_grams_and_production_report(self):
        mais = self.material("Maïs", qty="100", cost="1000")
        premix = self.material("Prémix", qty="5", cost="10000")
        res = self.post("/provenderie/formules/nouvelle", {
            "name": "Ponte test", "material_id": [str(mais), str(premix)],
            "quantity": ["49,5", "500"], "qty_unit": ["kg", "g"]})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:1500])
        f = self.one("SELECT * FROM formulas WHERE name = 'Ponte test'")
        self.assertAlmostEqual(f["base_qty"], 50)
        line = self.one("SELECT * FROM formula_lines WHERE material_id = ?", (premix,))
        self.assertAlmostEqual(line["quantity"], 0.5)
        self.assertEqual(line["input_unit"], "g")
        # unités incompatibles : refusé avec explication
        res = self.post("/provenderie/formules/nouvelle", {
            "name": "Mauvaise", "material_id": [str(mais)], "quantity": ["2"], "qty_unit": ["sac"]},
            follow_redirects=True)
        self.assertIn("impossible de", res.get_data(as_text=True))
        # fabrication d'un sac de 50 kg : stock diminué et associé prévenu
        self.post("/provenderie/fabrication/nouvelle", {"formula_id": str(f["id"]), "quantity": "50", "date": self.today()})
        self.assertAlmostEqual(self.stock(mais), 50.5)
        self.assertAlmostEqual(self.stock(premix), 4.5)
        msg = self.one("SELECT body FROM messages WHERE auto = 1 AND ref_type = 'production'")
        self.assertIn("Matières retirées du stock", msg[0])
        page = self.client.get("/matieres/").get_data(as_text=True)
        self.assertIn("Fabrication « Ponte test »", page)
