"""Plugin entry point: approved-account check, automatic signed update, then the E3DEX app."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
if not os.environ.get("E3D_BRIDGE_DATA_DIR") and os.environ.get("LOCALAPPDATA"):
    os.environ["E3D_BRIDGE_DATA_DIR"] = str(Path(os.environ["LOCALAPPDATA"]) / "E3DBridge")

if __name__ == "__main__":
    from e3d_loader.cli import main
    raise SystemExit(main(sys.argv[1:]))
