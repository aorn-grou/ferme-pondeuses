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


def _key(unit):
    return " ".join((unit or "").strip().lower().split()).rstrip(".")


def same_unit(a, b):
    return _key(a) == _key(b)


def convert(qty, from_unit, to_unit):
    """Convertit une quantité ; renvoie None si les unités ne sont pas compatibles."""
    if qty is None:
        return None
    if same_unit(from_unit, to_unit) or not _key(from_unit):
        return qty
    a, b = FACTORS.get(_key(from_unit)), FACTORS.get(_key(to_unit))
    if a and b and a[0] == b[0]:
        return qty * a[1] / b[1]
    return None


def to_kg(qty, unit):
    """Poids approximatif en kg (1 L compté comme 1 kg) ; None si impossible."""
    info = FACTORS.get(_key(unit))
    if info is None:
        return None
    return qty * info[1]


def js_table():
    return {k: [v[0], v[1]] for k, v in FACTORS.items()}
