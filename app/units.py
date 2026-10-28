"""Unités de mesure libres et conversions (g ↔ kg ↔ tonne, mL ↔ L).

Les unités inconnues (sac, dose, sachet…) restent utilisables : elles ne se
convertissent simplement qu'en elles-mêmes.
"""

# facteur vers l'unité de base de chaque famille (kg pour le poids, L pour le volume)
FACTORS = {
    "mg": ("poids", 0.000001), "milligramme": ("poids", 0.000001),
    "g": ("poids", 0.001), "gr": ("poids", 0.001), "gramme": ("poids", 0.001), "grammes": ("poids", 0.001),
    "kg": ("poids", 1.0), "kgs": ("poids", 1.0), "kilo": ("poids", 1.0), "kilos": ("poids", 1.0),
    "kilogramme": ("poids", 1.0), "kilogrammes": ("poids", 1.0),
    "t": ("poids", 1000.0), "tonne": ("poids", 1000.0), "tonnes": ("poids", 1000.0),
    "ml": ("volume", 0.001), "millilitre": ("volume", 0.001), "cl": ("volume", 0.01),
    "l": ("volume", 1.0), "litre": ("volume", 1.0), "litres": ("volume", 1.0),
}


import re

_PACK = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mg|g|gr|kg|kgs|kilos?|t|tonnes?|ml|cl|l|litres?)\b")


def _key(unit):
    return " ".join((unit or "").strip().lower().split()).rstrip(".")


def pack(unit):
    """Unité avec son contenu : « sac de 50 kg », « bidon 5 L », « sachet 500 g » → (famille, facteur), sinon None."""
    k = _key(unit)
    if k in FACTORS:
        return FACTORS[k]
    m = _PACK.search(k)
    if not m:
        return None
    base = FACTORS.get(m.group(2))
    if not base:
        return None
    return base[0], float(m.group(1).replace(",", ".")) * base[1]


def base_unit(unit):
    """L'unité de stock conseillée : « sac de 50 kg » → kg, « bidon 5 L » → L ; sinon l'unité telle quelle."""
    info = pack(unit)
    if info and _key(unit) not in FACTORS:
        return "kg" if info[0] == "poids" else "L"
    return " ".join((unit or "").split())


def same_unit(a, b):
    return _key(a) == _key(b)


def convert(qty, from_unit, to_unit):
    """Convertit une quantité ; renvoie None si les unités ne sont pas compatibles."""
    if qty is None:
        return None
    if same_unit(from_unit, to_unit) or not _key(from_unit):
        return qty
    a, b = pack(from_unit), pack(to_unit)
    if a and b and a[0] == b[0]:
        return qty * a[1] / b[1]
    return None


def to_kg(qty, unit):
    """Poids approximatif en kg (1 L compté comme 1 kg) ; None si impossible."""
    info = pack(unit)
    if info is None:
        return None
    return qty * info[1]


def js_table():
    return {k: [v[0], v[1]] for k, v in FACTORS.items()}
