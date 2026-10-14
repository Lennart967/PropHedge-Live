import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def load_config():
    env_path = os.environ.get("PROPHEDGE_CONFIG")
    path = Path(env_path) if env_path else BASE / "config.json"
    if not path.is_absolute():
        path = BASE / path
    if not path.exists():
        path = BASE / "config.example.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("prop", "live"):
        env = os.environ.get(f"{key.upper()}_PASSWORD")
        if env:
            cfg[key]["password"] = env
    db = Path(cfg.get("db_path", "prophedge.db"))
    cfg["db_path"] = str(db if db.is_absolute() else BASE / db)
    return cfg