"""Put keeper/ on sys.path so tests import the modules the way the CLI does."""
import sys
from pathlib import Path

KEEPER = Path(__file__).resolve().parents[1]
if str(KEEPER) not in sys.path:
    sys.path.insert(0, str(KEEPER))
