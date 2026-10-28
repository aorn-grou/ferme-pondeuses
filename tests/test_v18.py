"""Tests v0.18 : prix des œufs du jour, fumier, saisie rapide des morts, ordre des provendes,
recherche partout, menu par module, application installable."""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_finances import FinBase  # noqa: E402
from tests.test_stock import TestPermissionsAndReset  # noqa: E402


def ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def money(app, v):
    from app.utils import fmt_money
    with app.test_request_context():
        return fmt_money(v)


class TestPrixOeufs(FinBase):
    def test_daily_price_kept_and_used_by_sales(self):
        res = self.post("/prix-oeufs/changer", {"date": ago(10), "tray_price": "14 000", "unit": "plateau", "notes": "Marché"})
        self.assertEqual(res.status_code, 302)
        self.post("/prix-oeufs/changer", {"date": ago(3), "tray_price": "500", "unit": "oeuf"})  # 500 l'œuf = 15 000 le plateau
        self.assertEqual(self.one("SELECT COUNT(*) FROM egg_prices")[0], 2)
        from app.prices import price_on
        with self.app.test_request_context():
            self.assertEqual(price_on(ago(5)), 14000)
            self.assertEqual(price_on(), 15000)
            self.assertIsNone(price_on(ago(20)))
        # même jour : on corrige au lieu d'ajouter
        self.post("/prix-oeufs/changer", {"date": ago(3), "tray_price": "15500"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM egg_prices")[0], 2)
        # dans le futur ou 0 : refusé
        self.post("/prix-oeufs/changer", {"date": (date.today() + timedelta(days=1)).isoformat(), "tray_price": "16000"})
        self.post("/prix-oeufs/changer", {"date": ago(1), "tray_price": "0"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM egg_prices")[0], 2)
        # la nouvelle vente propose le prix du jour
        page = self.client.get("/ventes/nouvelle").get_data(as_text=True)
        self.assertIn('value="15500"', page)
        self.assertIn(money(self.app, 15500) + " le plateau", self.client.get("/ventes/").get_data(as_text=True))
        for vue in ("jour", "semaine", "mois", "annee", "nimporte"):
            self.assertEqual(self.client.get(f"/prix-oeufs/?vue={vue}").status_code, 200, vue)

    def test_period_stats_profit(self):
        a, _b = self.two_lots()
        self.post("/prix-oeufs/changer", {"date": ago(2), "tray_price": "15000"})
        self.post("/oeufs/", {"date": ago(1), "lot_id": [str(a["id"])], "trays": ["10"], "eggs": [""], "broken": [""], "notes": [""]})
        self.post("/ventes/nouvelle", {"date": ago(1), "product": "Œufs", "trays": "5", "eggs": "", "price": "15000",
                                       "price_unit": "plateau", "account_id": str(self.account())})
        from app.prices import period_stats
        with self.app.test_request_context():
            rows = period_stats("jour")
        day = next(r for r in rows if r["key"] == ago(1))
        self.assertEqual(day["sold"], 150)
        self.assertAlmostEqual(day["revenue"], 75000)
        self.assertAlmostEqual(day["sold_tray"], 15000)
        self.assertEqual(day["laid"], 300)
        self.assertAlmostEqual(day["avg"], 15000)
        self.assertAlmostEqual(day["profit"], day["revenue"] - day["costs"])


class TestFumier(FinBase):
    def test_remove_dry_sell_cancel(self):
        a, _b = self.two_lots()
        res = self.post("/fumier/enlever", {"date": ago(20), "lot_id": str(a["id"]), "quantity": "20", "unit": "sac", "dry_days": "14"})
        self.assertEqual(res.status_code, 302)
        self.post("/fumier/enlever", {"date": ago(2), "lot_id": "", "quantity": "8", "unit": "sac"})
        old = self.one("SELECT * FROM manure_batches WHERE quantity = 20")
        young = self.one("SELECT * FROM manure_batches WHERE quantity = 8")
        self.assertEqual(young["dry_days"], 14)
        before = self.balance()
        # pas encore sec : refusé
        self.post("/fumier/vendre", {"batch_id": str(young["id"]), "date": self.today(), "quantity": "2", "price": "3000",
                                     "account_id": str(self.account())})
        self.assertEqual(self.one("SELECT COUNT(*) FROM sales WHERE manure_id IS NOT NULL")[0], 0)
        # sec : 15 sacs à 3 000 Ar, l'argent entre dans la caisse
        self.post("/fumier/vendre", {"batch_id": str(old["id"]), "date": self.today(), "quantity": "15", "price": "3 000",
                                     "price_mode": "unite", "client_id": "__new__:Maraîcher Ivato", "paid": "",
                                     "account_id": str(self.account())})
        sale = self.one("SELECT * FROM sales WHERE manure_id = ?", (old["id"],))
        self.assertAlmostEqual(sale["total"], 45000)
        self.assertEqual(sale["lot_id"], a["id"])
        self.assertEqual(sale["is_eggs"], 0)
        self.assertAlmostEqual(self.balance(), before + 45000)
        self.assertIsNotNone(self.one("SELECT id FROM clients WHERE name = 'Maraîcher Ivato'"))
        # plus que le reste : refusé
        self.post("/fumier/vendre", {"batch_id": str(old["id"]), "date": self.today(), "quantity": "6", "price": "3000",
                                     "account_id": str(self.account())})
        self.assertEqual(self.one("SELECT COUNT(*) FROM sales WHERE manure_id IS NOT NULL")[0], 1)
        # le bénéfice du lot compte la vente de fumier
        from app.sales import lot_revenue
        with self.app.test_request_context():
            from app.db import query
            rev = lot_revenue(query("SELECT * FROM lots WHERE id = ?", (a["id"],), one=True))
        self.assertAlmostEqual(rev["other"], 45000)
        page = self.client.get("/fumier/").get_data(as_text=True)
        self.assertIn(money(self.app, 45000), page)
        # alerte « prêt à vendre » (il reste 5 sacs secs)
        self.assertIn("Fumier prêt à vendre", self.client.get("/").get_data(as_text=True))
        # un tas avec des ventes ne se supprime pas ; annuler la vente remet le fumier et reprend l'argent
        self.post(f"/fumier/{old['id']}/supprimer", {})
        self.assertIsNotNone(self.one("SELECT id FROM manure_batches WHERE id = ?", (old["id"],)))
        self.post(f"/ventes/{sale['id']}/annuler", {})
        self.assertEqual(self.one("SELECT COUNT(*) FROM sales WHERE manure_id IS NOT NULL")[0], 0)
        self.assertAlmostEqual(self.balance(), before)
        self.post(f"/fumier/{young['id']}/supprimer", {})
        self.assertIsNone(self.one("SELECT id FROM manure_batches WHERE id = ?", (young["id"],)))


class TestMortsRapides(FinBase):
    def test_quick_deaths_card(self):
        a, _b = self.two_lots()
        page = self.client.get(f"/lots/{a['id']}").get_data(as_text=True)
        self.assertIn('id="morts"', page)
        res = self.post(f"/lots/{a['id']}/effectif", {"kind": "mort", "quantity": "3", "date": self.today(), "notes": "Chaleur",
                                                      "anchor": "morts"})
        self.assertTrue(res.headers["Location"].endswith("#morts"))
        from app.lots import effectif
        with self.app.test_request_context():
            self.assertEqual(effectif(a["id"]), 997)
        page = self.client.get(f"/lots/{a['id']}").get_data(as_text=True)
        self.assertIn("Chaleur", page)
        res = self.post(f"/lots/{a['id']}/effectif", {"kind": "comptage", "quantity": "990", "date": self.today()})
        self.assertTrue(res.headers["Location"].endswith("#effectif"))
        with self.app.test_request_context():
            self.assertEqual(effectif(a["id"]), 990)


class TestOrdreProvende(FinBase):
    def test_feeds_follow_growth_stage(self):
        mais = self.material("Maïs", qty="1000", cost="1000")
        soja = self.material("Soja", qty="1000", cost="2000")
        for name in ("Ponte", "Croissance", "Démarrage"):
            self.post("/provenderie/formules/nouvelle", {"name": name, "material_id": [str(soja), str(mais)], "quantity": ["10", "90"]})
        from app.stock import formula_lines, formulas_overview
        with self.app.test_request_context():
            names = [f["name"] for f in formulas_overview()]
            fid = self.one("SELECT id FROM formulas WHERE name = 'Ponte'")[0]
            lines = [l["name"] for l in formula_lines(fid)]
        self.assertEqual(names, ["Démarrage", "Croissance", "Ponte"])
        self.assertEqual(lines, ["Soja", "Maïs"])  # dans l'ordre où la recette a été écrite


class TestRechercheEtMenu(TestPermissionsAndReset):
    def test_search_respects_rights(self):
        mais = self.material("Maïs grain", qty="10", cost="1000")
        res = self.client.get("/recherche/rapide?q=mais").get_json()
        self.assertTrue(any(r["label"] == "Maïs grain" and r["url"].endswith(f"/matieres/matiere/{mais}") for r in res))
        self.assertTrue(any(r["label"] == "Fumier" for r in self.client.get("/recherche/rapide?q=fumier").get_json()))
        self.assertEqual(self.client.get("/recherche/?q=caisse").status_code, 200)
        self.assertEqual(self.client.get("/recherche/rapide?q=a").get_json(), [])
        vendeur = self.make_user("lova", "ventes")
        res = vendeur.get("/recherche/rapide?q=mais").get_json()
        self.assertFalse(any(r["label"] == "Maïs grain" for r in res))  # pas le droit aux matières

    def test_module_menu_and_install(self):
        page = self.client.get("/lots/").get_data(as_text=True)
        self.assertIn("data-modnav", page)
        self.assertIn("Menu principal", page)
        self.assertIn("Nouveau lot", page)
        self.assertNotIn("data-modnav", self.client.get("/").get_data(as_text=True))  # tableau de bord : menu principal
        menu = self.client.get("/").get_data(as_text=True)
        self.assertIn("Stock matières premières", menu)
        self.assertIn("Stock provende finie", menu)
        self.assertIn('class="content form-page"', self.client.get("/lots/nouveau").get_data(as_text=True))
        m = self.client.get("/manifest.webmanifest")
        self.assertEqual(m.status_code, 200)
        self.assertEqual(m.get_json()["display"], "standalone")
        sw = self.client.get("/sw.js")
        self.assertEqual(sw.headers["Service-Worker-Allowed"], "/")
        self.assertEqual(self.client.get("/hors-ligne").status_code, 200)
        self.assertEqual(self.client.get("/installer").status_code, 200)
        anon = self.app.test_client()
        self.assertEqual(anon.get("/manifest.webmanifest").status_code, 200)
        self.assertEqual(anon.get("/recherche/rapide?q=lot").status_code, 302)


if __name__ == "__main__":
    unittest.main()


class TestUnitesLibres(FinBase):
    def test_free_units_in_purchase_and_formula(self):
        mais = self.material("Maïs", qty="100", cost="1000")
        # achat de 3 sacs de 50 kg à 60 000 Ar le sac (matière comptée en kg)
        res = self.post("/matieres/achats/nouveau", {
            "date": self.today(), "supplier_name": "", "material_id": [str(mais)], "quantity": ["3"],
            "line_unit": ["sac de 50 kg"], "unit_price": ["60 000"], "pay_mode": "tout", "account_id": str(self.account())})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:500])
        self.assertAlmostEqual(self.stock(mais), 250)
        line = self.one("SELECT * FROM purchase_lines")
        self.assertAlmostEqual(line["quantity"], 150)
        self.assertAlmostEqual(line["unit_price"], 1200)
        self.assertAlmostEqual(line["total"], 180000)
        # « sac » sans poids : message clair, rien n'est enregistré
        res = self.post("/matieres/achats/nouveau", {
            "date": self.today(), "material_id": [str(mais)], "quantity": ["2"], "line_unit": ["sac"], "unit_price": ["60000"],
            "pay_mode": "tout", "account_id": str(self.account())}, follow_redirects=True)
        self.assertIn("sac de 50 kg", res.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM purchases")[0], 1)
        # nouvelle matière écrite librement avec « sac de 25 kg » : stock en kg
        self.post("/matieres/achats/nouveau", {
            "date": self.today(), "material_id": ["__new__:Soja|sac de 25 kg"], "quantity": ["4"], "line_unit": ["sac de 25 kg"],
            "unit_price": ["50000"], "pay_mode": "tout", "account_id": str(self.account())})
        soja = self.one("SELECT * FROM materials WHERE name = 'Soja'")
        self.assertEqual(soja["unit"], "kg")
        self.assertAlmostEqual(self.stock(soja["id"]), 100)
        # formule : matière nouvelle écrite avec son unité, quantités en sacs et en g
        res = self.post("/provenderie/formules/nouvelle", {
            "name": "Ponte", "material_id": [str(mais), "__new__:Coquillage|kg", str(soja["id"])],
            "quantity": ["1", "5", "500"], "qty_unit": ["sac de 50 kg", "kg", "g"]})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:800])
        f = self.one("SELECT * FROM formulas WHERE name = 'Ponte'")
        self.assertAlmostEqual(f["base_qty"], 55.5)
        self.assertAlmostEqual(self.one("SELECT quantity FROM formula_lines WHERE material_id = ?", (mais,))[0], 50)
