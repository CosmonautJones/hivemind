import os
import sys
from pathlib import Path

os.environ["FORGE_NO_AUTOAPP"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hub"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
