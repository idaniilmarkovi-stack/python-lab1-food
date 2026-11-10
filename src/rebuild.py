"""Проверка воспроизводимости: очищает interim и processed и строит их заново из raw.

    python -m src.rebuild                # весь набор + базы данных
    python -m src.rebuild --no-db        # без загрузки в СУБД
    python -m src.rebuild --mode debug --no-db
Каталог raw не затрагивается.
"""
import argparse
import logging
import shutil
import sys

from . import config as cfg
from . import io_utils as io
from . import pipeline

log = logging.getLogger("rebuild")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("full", "debug"), default="full")
    ap.add_argument("--no-db", action="store_true")
    a = ap.parse_args(argv)
    io.setup_logging("rebuild")
    for d in (cfg.INTERIM, cfg.PROCESSED):
        if d.exists():
            for f in d.iterdir():
                if a.mode == "debug" and "debug" not in f.name:
                    continue
                shutil.rmtree(f) if f.is_dir() else f.unlink()
    log.info("interim и processed очищены; raw не тронут")
    rc = pipeline.main(["--mode", a.mode])
    if rc == 0 and not a.no_db and a.mode == "full":
        from . import db
        rc = db.main(["sqlite"])
    return rc


if __name__ == "__main__":
    sys.exit(main())
