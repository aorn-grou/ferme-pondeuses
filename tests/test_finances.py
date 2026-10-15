"""Tests des modules Dépenses, Caisse & propriétaires, Personnel, Immobilisations, Réformes, Import Excel,
et du moteur de partage des coûts entre les lots."""
import io
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_lots import TestLots  # noqa: E402
from tests.test_stock import StockBase  # noqa: E402


class FinBase(StockBase):
    def two_lots(self):
        dem, ponte, prog = TestLots.setup_feed(self)
        d = (date.today() - timedelta(days=40)).isoformat()
        TestLots.new_lot(self, prog, d, count="1000", paid="0", name="Lot A")
        TestLots.new_lot(self, prog, d, count="500", paid="0", name="Lot B")
        a = self.one("SELECT * FROM lots WHERE name = 'Lot A'")
        b = self.one("SELECT * FROM lots WHERE name = 'Lot B'")
        return a, b

    def balance(self, name="Caisse de la ferme"):
        return self.one("SELECT COALESCE(SUM(amount), 0) FROM cash_movements WHERE account_id = ?", (self.account(name),))[0]

    def costs(self, lot_id):
        from app.costs import lot_costs
        with self.app.test_request_context():
            from flask import g
            g.pop("_alloc", None)
            from app.db import query
            return lot_costs(query("SELECT * FROM lots WHERE id = ?", (lot_id,), one=True))


class TestDepenses(FinBase):
    def test_expenses_shared_and_unpaid(self):
        a, b = self.two_lots()
        t = self.today()
        before = self.balance()
        # dépense commune payée : partagée 2/3 - 1/3 (1000 et 500 poules)
        res = self.post("/depenses/nouvelle", {"date": t, "category": "Électricité", "label": "JIRAMA", "amount": "90 000",
                                               "lot_id": "", "account_id": str(self.account()), "beneficiary": "", "notes": ""})
        self.assertEqual(res.status_code, 302)
        self.assertAlmostEqual(self.balance(), before - 90000)
        self.assertAlmostEqual(self.costs(a["id"])["expenses"], 60000)
        self.assertAlmostEqual(self.costs(b["id"])["expenses"], 30000)
        # dépense d'un seul lot, pas encore payée : rien ne sort de la caisse
        self.post("/depenses/nouvelle", {"date": t, "category": "Litière / copeaux", "amount": "20000", "lot_id": str(b["id"]),
                                         "account_id": str(self.account()), "unpaid": "1"})
        self.assertAlmostEqual(self.balance(), before - 90000)
        self.assertAlmostEqual(self.costs(b["id"])["expenses"], 50000)
        exp = self.one("SELECT * FROM expenses WHERE status = 'a_payer'")
        page = self.client.get("/depenses/?voir=a_payer").get_data(as_text=True)
        self.assertIn("Litière", page)
        # payer plus tard
        self.post(f"/depenses/{exp['id']}/payer", {"date": t, "account_id": str(self.account())})
        self.assertAlmostEqual(self.balance(), before - 110000)
        # sans caisse et sans « à payer » : refusé
        n = self.one("SELECT COUNT(*) FROM expenses")[0]
        self.post("/depenses/nouvelle", {"date": t, "category": "Eau", "amount": "5000", "account_id": ""})
        self.assertEqual(self.one("SELECT COUNT(*) FROM expenses")[0], n)
        # dans le futur : refusé
        self.post("/depenses/nouvelle", {"date": (date.today() + timedelta(days=2)).isoformat(), "category": "Eau", "amount": "5000",
                                         "account_id": str(self.account())})
        self.assertEqual(self.one("SELECT COUNT(*) FROM expenses")[0], n)
        # corriger le montant : la caisse suit
        e1 = self.one("SELECT id FROM expenses WHERE category = 'Électricité'")[0]
        self.post(f"/depenses/{e1}/modifier", {"date": t, "category": "Électricité", "label": "JIRAMA", "amount": "100000",
                                              "lot_id": "", "account_id": str(self.account())})
        self.assertAlmostEqual(self.balance(), before - 120000)
        # supprimer : l'argent revient
        self.post(f"/depenses/{e1}/supprimer", {})
        self.assertAlmostEqual(self.balance(), before - 20000)
        for url in ["/depenses/", "/depenses/?voir=tout", f"/depenses/{exp['id']}/modifier",
                    f"/lots/{a['id']}", "/rapports/", "/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertIn("Le détail des dépenses du lot", self.client.get(f"/lots/{b['id']}").get_data(as_text=True))


class TestCaisse(FinBase):
    def test_owner_moves_transfer_count(self):
        t = self.today()
        caisse, mm = self.account(), self.account("Mobile money")
        p1, p2 = self.account("Propriétaire 1"), self.account("Propriétaire 2")
        self.post("/caisse/operation", {"op": "apport", "date": t, "owner_id": str(p1), "account_id": str(caisse), "amount": "1 000 000"})
        self.post("/caisse/operation", {"op": "apport", "date": t, "owner_id": str(p2), "account_id": str(caisse), "amount": "600000"})
        self.post("/caisse/operation", {"op": "retrait", "date": t, "owner_id": str(p1), "account_id": str(caisse), "amount": "100000"})
        self.assertAlmostEqual(self.balance(), 1500000)
        self.assertAlmostEqual(self.balance("Propriétaire 1"), -900000)  # a mis 900 000 net
        self.post("/caisse/operation", {"op": "transfert", "date": t, "from_id": str(caisse), "account_id": str(mm), "amount": "200000"})
        self.assertAlmostEqual(self.balance(), 1300000)
        self.assertAlmostEqual(self.balance("Mobile money"), 200000)
        # transfert vers la même caisse : refusé
        self.post("/caisse/operation", {"op": "transfert", "date": t, "from_id": str(caisse), "account_id": str(caisse), "amount": "5"})
        self.assertAlmostEqual(self.balance(), 1300000)
        # comptage : il manque 30 000 → écart dans Écarts & contrôles
        self.post("/caisse/operation", {"op": "comptage", "date": t, "account_id": str(caisse), "counted": "1270000", "notes": ""})
        self.assertAlmostEqual(self.balance(), 1270000)
        page = self.client.get("/controles/").get_data(as_text=True)
        self.assertIn("Écart de caisse", page)
        gap = self.one("SELECT id FROM cash_movements WHERE kind = 'ecart_caisse'")[0]
        self.post("/controles/expliquer", {"table": "cash_movements", "id": str(gap), "why": "Erreur de rendu de monnaie"})
        self.assertEqual(self.one("SELECT notes FROM cash_movements WHERE id = ?", (gap,))[0], "Erreur de rendu de monnaie")
        # comptage juste : pas de mouvement
        n = self.one("SELECT COUNT(*) FROM cash_movements")[0]
        self.post("/caisse/operation", {"op": "comptage", "date": t, "account_id": str(caisse), "counted": "1270000"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM cash_movements")[0], n)
        # parts : doivent faire 100 %
        self.post("/caisse/parts", {f"share_{p1}": "70", f"share_{p2}": "20"})
        self.assertEqual(self.one("SELECT share FROM accounts WHERE id = ?", (p1,))[0], 0)
        self.post("/caisse/parts", {f"share_{p1}": "60", f"share_{p2}": "40"})
        self.assertEqual(self.one("SELECT share FROM accounts WHERE id = ?", (p1,))[0], 60)
        page = self.client.get("/caisse/").get_data(as_text=True)
        self.assertIn("A mis dans la ferme", page)
        self.assertIn("900", page)
        # supprimer un apport supprime ses deux côtés
        ap = self.one("SELECT id FROM cash_movements WHERE kind = 'apport' AND account_id = ?", (p2,))[0]
        self.post(f"/caisse/mouvement/{ap}/supprimer", {})
        self.assertAlmostEqual(self.balance("Propriétaire 2"), 0)
        self.assertAlmostEqual(self.balance(), 670000)
        for url in ["/caisse/", f"/caisse/compte/{caisse}", f"/caisse/compte/{p1}", "/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class TestPersonnel(FinBase):
    def test_salary_advance_daily(self):
        a, b = self.two_lots()
        month = self.today()[:7]
        first = month + "-01"
        self.post("/personnel/employe/nouveau", {"name": "Rasoa", "job": "Soigneuse", "pay_type": "mois", "salary": "300000",
                                                 "start_date": (date.fromisoformat(first) - timedelta(days=60)).isoformat()})
        self.post("/personnel/employe/nouveau", {"name": "Koto", "job": "Gardien", "pay_type": "jour", "salary": "8000",
                                                 "start_date": (date.fromisoformat(first) - timedelta(days=60)).isoformat()})
        ras = self.one("SELECT id FROM employees WHERE name = 'Rasoa'")[0]
        koto = self.one("SELECT * FROM employees WHERE name = 'Koto'")
        # avance puis salaire : reste à payer
        self.post("/personnel/paiement", {"employee_id": str(ras), "kind": "avance", "month": month, "date": self.today(),
                                          "amount": "50000", "account_id": str(self.account())})
        from app.staff import month_sheet
        with self.app.test_request_context():
            from app.db import query
            s = month_sheet(query("SELECT * FROM employees WHERE id = ?", (ras,), one=True), month)
        self.assertAlmostEqual(s["due"], 300000)
        self.assertAlmostEqual(s["rest"], 250000)
        self.post("/personnel/paiement", {"employee_id": str(ras), "kind": "salaire", "month": month, "date": self.today(),
                                          "amount": "250000", "account_id": str(self.account())})
        self.assertAlmostEqual(self.balance(), -300000)
        # journalier : jours du mois − absences
        self.post("/personnel/absence", {"employee_id": str(koto["id"]), "date": first, "days": "2", "reason": "malade"})
        with self.app.test_request_context():
            from app.db import query
            s = month_sheet(query("SELECT * FROM employees WHERE id = ?", (koto["id"],), one=True), month)
        days = date.fromisoformat(self.today()).day
        self.assertEqual(s["worked"], days - 2)
        self.assertAlmostEqual(s["due"], (days - 2) * 8000)
        # les salaires sont partagés entre les lots (2/3, 1/3)
        self.assertAlmostEqual(self.costs(a["id"])["salaries"], 200000)
        self.assertAlmostEqual(self.costs(b["id"])["salaries"], 100000)
        # annuler un paiement : l'argent revient
        pay = self.one("SELECT id FROM staff_payments WHERE kind = 'avance'")[0]
        self.post(f"/personnel/paiement/{pay}/supprimer", {})
        self.assertAlmostEqual(self.balance(), -250000)
        # départ
        self.post(f"/personnel/employe/{koto['id']}/depart", {"end_date": self.today()})
        self.assertEqual(self.one("SELECT active FROM employees WHERE id = ?", (koto["id"],))[0], 0)
        for url in ["/personnel/", f"/personnel/?mois={month}", f"/personnel/employe/{ras}", f"/personnel/employe/{ras}/modifier",
                    "/personnel/employe/nouveau"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class TestImmobilisations(FinBase):
    def test_wear_shared(self):
        a, b = self.two_lots()
        bought = (date.today() - timedelta(days=29)).isoformat()  # 30 jours d'usure
        res = self.post("/immobilisations/nouveau", {"name": "Cages", "category": "Cages / batteries", "purchase_date": bought,
                                                     "cost": "3 650 000", "life_years": "10", "residual": "", "lot_id": "",
                                                     "account_id": str(self.account()), "notes": ""})
        self.assertEqual(res.status_code, 302)
        asset = self.one("SELECT * FROM assets")
        self.assertAlmostEqual(self.balance(), -3650000)
        from app.costs import asset_state
        with self.app.test_request_context():
            st = asset_state(asset)
        self.assertAlmostEqual(st["daily"], 1000)
        self.assertAlmostEqual(st["used"], 30000)
        total = self.costs(a["id"])["wear"] + self.costs(b["id"])["wear"]
        self.assertAlmostEqual(total, 30000, delta=1)
        self.assertAlmostEqual(self.costs(a["id"])["wear"], 20000, delta=1)
        # vendu : l'usure s'arrête, l'argent entre
        self.post(f"/immobilisations/{asset['id']}/sortie", {"end_date": self.today(), "sale_price": "3000000",
                                                            "account_id": str(self.account()), "reason": "vendu"})
        self.assertAlmostEqual(self.balance(), -650000)
        self.assertEqual(self.one("SELECT status FROM assets")[0], "sorti")
        for url in ["/immobilisations/", f"/immobilisations/{asset['id']}"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        # valeur à la fin plus grande que le prix : refusé
        n = self.one("SELECT COUNT(*) FROM assets")[0]
        self.post("/immobilisations/nouveau", {"name": "Moto", "purchase_date": bought, "cost": "100", "life_years": "5", "residual": "200"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM assets")[0], n)


class TestReformes(FinBase):
    def test_reform_sale(self):
        a, b = self.two_lots()
        t = self.today()
        res = self.post("/reformes/vendre", {"lot_id": str(b["id"]), "date": t, "birds": "100", "price_mode": "poule", "price": "12000",
                                             "client_id": "__new__:Boucherie Rado", "paid": "", "account_id": str(self.account()), "notes": ""})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True)[:500])
        from app.lots import effectif
        with self.app.test_request_context():
            self.assertEqual(effectif(b["id"]), 400)
        sale = self.one("SELECT * FROM sales WHERE lot_event_id IS NOT NULL")
        self.assertAlmostEqual(sale["total"], 1200000)
        self.assertAlmostEqual(self.balance(), 1200000)
        self.assertEqual(self.one("SELECT name FROM clients WHERE id = ?", (sale["client_id"],))[0], "Boucherie Rado")
        # compte dans les ventes du lot
        from app.sales import lot_revenue
        with self.app.test_request_context():
            from app.db import query
            rev = lot_revenue(query("SELECT * FROM lots WHERE id = ?", (b["id"],), one=True))
        self.assertAlmostEqual(rev["other"], 1200000)
        # trop de poules : refusé
        n = self.one("SELECT COUNT(*) FROM sales")[0]
        self.post("/reformes/vendre", {"lot_id": str(b["id"]), "date": t, "birds": "401", "price_mode": "poule", "price": "1"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM sales")[0], n)
        # tout vendre au kg, à crédit : le lot se termine
        self.post("/reformes/vendre", {"lot_id": str(b["id"]), "date": t, "birds": "400", "price_mode": "kg", "price": "7000",
                                       "weight": "720", "paid": "0", "account_id": str(self.account()), "close": "1"})
        self.assertEqual(self.one("SELECT status FROM lots WHERE id = ?", (b["id"],))[0], "termine")
        last = self.one("SELECT * FROM sales ORDER BY id DESC")
        self.assertAlmostEqual(last["total"], 5040000)
        self.assertAlmostEqual(last["paid"], 0)
        # annuler la vente : les poules reviennent
        self.post(f"/ventes/{sale['id']}/annuler", {})
        with self.app.test_request_context():
            self.assertEqual(effectif(b["id"]), 100)
        for url in ["/reformes/", "/ventes/", f"/ventes/{last['id']}"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class TestImport(FinBase):
    def xlsx(self, header, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(header)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    def send(self, kind, buf, name="f.xlsx"):
        self.client.get("/import/")
        with self.client.session_transaction() as s:
            tok = s.get("_csrf")
        return self.client.post("/import/verifier", data={"_csrf": tok, "kind": kind, "file": (buf, name)},
                                content_type="multipart/form-data")

    def confirm(self, page):
        import re
        token = re.search(r'name="token" value="([0-9a-f]+)"', page).group(1)
        return self.post("/import/confirmer", {"token": token})

    def test_import_eggs_expenses_sales_undo(self):
        a, b = self.two_lots()
        d1 = (date.today() - timedelta(days=3)).strftime("%d/%m/%Y")
        d2 = (date.today() - timedelta(days=2)).strftime("%d/%m/%Y")
        # modèle téléchargeable
        res = self.client.get("/import/modele/oeufs")
        self.assertEqual(res.status_code, 200)
        self.assertGreater(len(res.data), 2000)
        # œufs : 2 bonnes lignes, 1 lot inconnu, 1 doublon
        buf = self.xlsx(["Date *", "Lot *", "Bons œufs *", "Cassés", "Remarque"],
                        [[d1, "Lot A", 900, 5, ""], [d2, "lot a", 910, 3, ""], [d1, "Lot Z", 100, 0, ""], [d1, "Lot A", 1, 0, ""]])
        page = self.send("oeufs", buf).get_data(as_text=True)
        self.assertIn("lot « Lot Z » inconnu", page)
        self.assertIn("deux fois", page)
        self.confirm(page)
        self.assertEqual(self.one("SELECT COUNT(*) FROM egg_collections")[0], 2)
        imp = self.one("SELECT * FROM imports")
        self.assertEqual(imp["rows"], 2)
        # dépenses en CSV, compte inconnu créé, vide = à payer
        csv_text = "Date;Poste;Détail;Montant;Lot;Compte;Payé à;Remarque\n" + f"{d1};Transport;;15000;;Caisse de la ferme;;\n" + \
                   f"{d2};Eau;;8000;Lot B;;;\n{d2};Gaz;;5000;;Banque BOA;;\n"
        page = self.send("depenses", io.BytesIO(csv_text.encode("utf-8")), "d.csv").get_data(as_text=True)
        self.confirm(page)
        self.assertEqual(self.one("SELECT COUNT(*) FROM expenses")[0], 3)
        self.assertEqual(self.one("SELECT COUNT(*) FROM expenses WHERE status = 'a_payer'")[0], 1)
        self.assertIsNotNone(self.one("SELECT id FROM accounts WHERE name = 'Banque BOA'"))
        # ventes : plateaux → œufs
        buf = self.xlsx(["Date", "Client", "Produit", "Quantité", "Unité", "Prix unitaire", "Payé", "Compte", "Lot", "Remarque"],
                        [[d2, "Hôtel X", "Œufs", 10, "plateau", 15000, 100000, "Caisse de la ferme", "", ""]])
        self.confirm(self.send("ventes", buf).get_data(as_text=True))
        sale = self.one("SELECT * FROM sales")
        self.assertEqual(sale["quantity"], 300)
        self.assertAlmostEqual(sale["total"], 150000)
        self.assertAlmostEqual(sale["paid"], 100000)
        # annuler l'import des dépenses : tout disparaît, l'argent revient
        dep_imp = self.one("SELECT id FROM imports WHERE kind = 'depenses'")[0]
        self.post(f"/import/{dep_imp}/annuler", {})
        self.assertEqual(self.one("SELECT COUNT(*) FROM expenses")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM cash_movements WHERE kind = 'depense'")[0], 0)
        # colonnes manquantes : refusé avec explication
        res = self.send("mortalite", self.xlsx(["Date", "Nombre"], [[d1, 3]]))
        self.assertEqual(res.status_code, 302)
        for url in ["/import/", "/import/modele/ventes", "/import/modele/depenses", "/import/modele/mortalite"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class TestPages(FinBase):
    def test_all_finance_pages_and_reports(self):
        a, b = self.two_lots()
        for url in ["/", "/depenses/", "/caisse/", "/personnel/", "/immobilisations/", "/reformes/", "/import/", "/rapports/",
                    f"/rapports/?lot={a['id']}", f"/lots/{a['id']}", "/controles/", "/guide"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        # chaque module « prêt » du menu ouvre sa vraie page
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("bientôt", page)


if __name__ == "__main__":
    unittest.main()
