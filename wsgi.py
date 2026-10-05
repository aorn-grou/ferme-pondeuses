"""Point d'entrée pour PythonAnywhere (ou tout serveur WSGI)."""
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from app import create_app  # noqa: E402

application = create_app()
