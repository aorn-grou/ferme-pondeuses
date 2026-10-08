"""Tests du module Lots de poules et de l'alimentation."""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_stock import StockBase  # noqa: E402


class TestLots(StockBase):
    def setup_feed(self):
        mais = self.material("Maïs", qty="1000", cost="1000")
        self.post("/provenderie/formules/nouvelle", {"name": "Démarrage", "material_id": [str(mais)], "quantity": ["100"]})
        self.post("/provenderie/formules/nouvelle", {"name": "Ponte", "material_id": [str(mais)], "quantity": ["100"]})
        dem = self.one("SELECT id FROM formulas WHERE name = 'Démarrage'")[0]
        ponte = self.one("SELECT id FROM formulas WHERE name = 'Ponte'")[0]
        self.post("/provenderie/fabrication/nouvelle", {"formula_id": str(dem), "quantity": "200", "date": "2026-10-01"})
        self.post("/provenderie/programmes/nouveau", {
            "name": "Programme ponte", "week_from": ["1", "3"], "week_to": ["2", "80"],
            "formula_id": [str(dem), str(ponte)], "grams_per_bird": ["20", "110"]})
        prog = self.one("SELECT id FROM feed_programs")[0]
        return dem, ponte, prog

    def new_lot(self, prog, arrival, count="500", **extra):
        data = {"name": extra.pop("name", "Lot A"), "arrival_date": arrival, "initial_count": count,
                "program_id": str(prog), "reform_week": "72", "chick_price": "2000", "other_costs": "50000",
                "account_id": str(self.account()), **extra}
        return self.post("/lots/nouveau", data)

    def test_full_flow(self):
        dem, ponte, prog = self.setup_feed()
        arrival = (date.today() - timedelta(days=3)).isoformat()
        res = self.new_lot(prog, arrival)
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:1500])
        lot = self.one("SELECT * FROM lots")
        # achat des poussins : 500 × 2000 + 50 000 sorti de la caisse
        self.assertAlmostEqual(self.one("SELECT SUM(amount) FROM cash_movements WHERE ref_type = 'lot'")[0], -1050000)
        # mortalité
        self.post(f"/lots/{lot['id']}/effectif", {"kind": "mort", "quantity": "4", "date": date.today().isoformat()})
        from app.lots import effectif
        with self.app.test_request_context():
            self.assertEqual(effectif(lot["id"]), 496)
        # la fiche propose Démarrage : 496 × 20 g = 9,92 kg
        page = self.client.get(f"/lots/{lot['id']}").get_data(as_text=True)
        self.assertIn("9,92", page)
        self.assertIn("Démarrage", page)
        # distribution : le stock de provende baisse
        self.post(f"/lots/{lot['id']}/provende", {"formula_id": str(dem), "quantity": "9,92", "date": date.today().isoformat()})
        self.assertAlmostEqual(self.feed(dem), 200 - 9.92)
        # plus que le stock : refusé avec explication
        res = self.post(f"/lots/{lot['id']}/provende", {"formula_id": str(dem), "quantity": "500",
                                                        "date": date.today().isoformat()}, follow_redirects=True)
        self.assertIn("il ne reste que", res.get_data(as_text=True))
        # provende différente du programme : acceptée avec une simple note
        self.post("/provenderie/mouvement", {"kind": "entree", "formula_id": str(ponte), "quantity": "50",
                                             "date": "2026-10-01", "unit_cost": "1500"})
        res = self.post(f"/lots/{lot['id']}/provende", {"formula_id": str(ponte), "quantity": "5",
                                                        "date": date.today().isoformat()}, follow_redirects=True)
        self.assertIn("le programme prévoit", res.get_data(as_text=True))
        self.assertAlmostEqual(self.feed(ponte), 45)
        # prévision jusqu'à la réforme : surtout de la Ponte
        from app.lots import forecast
        with self.app.test_request_context():
            fc = forecast(self.one("SELECT * FROM lots"))
        self.assertIn("Ponte", fc["by_formula"])
        self.assertGreater(fc["by_formula"]["Ponte"]["kg"], 20000)
        # annulation d'une distribution : la provende revient
        fd = self.one("SELECT id FROM feedings WHERE formula_id = ?", (ponte,))[0]
        self.post(f"/lots/provende/{fd}/supprimer", {})
        self.assertAlmostEqual(self.feed(ponte), 50)
        # l'associé est prévenu
        self.assertGreaterEqual(self.one("SELECT COUNT(*) FROM messages WHERE auto = 1 AND ref_type = 'lot'")[0], 3)
        # pages principales
        for url in ("/lots/", "/lots/alimentation", f"/lots/{lot['id']}/modifier", "/lots/nouveau"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_feeding_day_and_counting(self):
        dem, _ponte, prog = self.setup_feed()
        arrival = date.today().isoformat()
        self.new_lot(prog, arrival, count="100", name="Lot B", account_id="", other_costs="0", chick_price="0")
        lot = self.one("SELECT * FROM lots")
        self.post("/lots/alimentation", {"date": arrival, "lot_id": [str(lot["id"])], "formula_id": [str(dem)],
                                         "quantity": ["2"]})
        self.assertAlmostEqual(self.feed(dem), 198)
        # comptage : 97 poules comptées → -3
        self.post(f"/lots/{lot['id']}/effectif", {"kind": "comptage", "quantity": "97", "date": arrival})
        self.assertEqual(self.one("SELECT quantity FROM lot_events")[0], -3)
        # pas de poules négatives
        res = self.post(f"/lots/{lot['id']}/effectif", {"kind": "mort", "quantity": "500", "date": arrival},
                        follow_redirects=True)
        self.assertIn("Impossible", res.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()


class TestControles(TestLots):
    def test_gaps_page(self):
        son = self.material("Son de riz", qty="400", cost="450")
        mais = self.material("Maïs jaune", qty="100", cost="1000")
        # écart expliqué et écart sans explication
        self.post("/matieres/inventaire", {"date": date.today().isoformat(), f"count_{son}": "390", f"why_{son}": "Rats",
                                           f"count_{mais}": "95"})
        _dem, _ponte, prog = self.setup_feed()
        self.new_lot(prog, (date.today() - timedelta(days=30)).isoformat(), account_id="", other_costs="0", paid="0")
        lot = self.one("SELECT id FROM lots")[0]
        self.post(f"/lots/{lot}/effectif", {"kind": "comptage", "quantity": "490", "date": date.today().isoformat()})
        self.post(f"/lots/{lot}/effectif", {"kind": "mort", "quantity": "12", "date": date.today().isoformat(),
                                            "notes": "Chaleur"})
        page = self.client.get("/controles/?periode=30").get_data(as_text=True)
        self.assertIn("Rats", page)
        self.assertIn("Écart de comptage", page)
        self.assertIn("Mortalité anormale", page)
        self.assertIn("sans explication", page)
        # ajouter une explication après coup
        mv = self.one("SELECT id FROM stock_moves WHERE material_id = ? AND kind = 'inventaire'", (mais,))[0]
        self.post("/controles/expliquer", {"table": "stock_moves", "id": str(mv), "why": "Erreur de pesée"})
        self.assertEqual(self.one("SELECT notes FROM stock_moves WHERE id = ?", (mv,))[0], "Erreur de pesée")
        # le tableau de bord montre le bandeau
        self.assertIn("Écarts &amp; contrôles", self.client.get("/").get_data(as_text=True))
