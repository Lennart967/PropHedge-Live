import multiprocessing

import uvicorn

from app.config import load_config

if __name__ == "__main__":
    multiprocessing.freeze_support()
    cfg = load_config()
    uvicorn.run("app.main:app", host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 8000)), workers=1)
