"""Сквозной запуск обработки одной командой.

    python -m src.pipeline                          # полный набор: все этапы
    python -m src.pipeline --mode debug             # отладочный срез (200 тыс. строк)
    python -m src.pipeline --stage clean --mode debug
    python -m src.pipeline --input data/raw/branded_food.csv --output data/processed/clean.parquet

Этапы: serialize (CSV -> source.parquet), type (CSV + схема -> source_typed.parquet),
clean (source_typed.parquet -> clean.parquet). Каталог raw только читается.
"""
import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd

from . import cleaning as cl
from . import config as cfg
from . import io_utils as io

log = logging.getLogger("pipeline")
STAGES = ("serialize", "type", "clean")


def paths_for(mode: str, input_path: Path | None, output_path: Path | None) -> dict:
    sfx = "_debug" if mode == "debug" else ""
    src = Path(input_path) if input_path else (cfg.DEBUG_SLICE if mode == "debug" else cfg.RAW_BRANDED)
    return dict(
        input=src,
        source=cfg.INTERIM / f"source{sfx}.parquet",
        typed=cfg.INTERIM / f"source_typed{sfx}.parquet",
        output=Path(output_path) if output_path else cfg.PROCESSED / f"clean{sfx}.parquet",
        results=cfg.RESULTS / ("debug" if mode == "debug" else "full"),
    )


def stage_serialize(p: dict) -> None:
    t0 = time.perf_counter()
    df = io.read_csv_naive(p["input"])
    log.info("serialize: прочитано %d x %d за %.1f c", *df.shape, time.perf_counter() - t0)
    io.write_parquet(df, p["source"])


def stage_type(p: dict) -> None:
    t0 = time.perf_counter()
    df = io.read_csv_typed(p["input"])
    log.info("type: прочитано %d x %d за %.1f c", *df.shape, time.perf_counter() - t0)
    io.write_parquet(df, p["typed"])


def stage_clean(p: dict) -> pd.DataFrame:
    df = io.read_parquet(p["typed"])
    n0, c0 = df.shape
    stats = cl.Stats()
    out = cl.clean_all(df, stats, p["results"], cfg.RAW_FOOD)
    p["results"].mkdir(parents=True, exist_ok=True)
    frame = stats.frame()
    frame.to_csv(p["results"] / "cleaning_stats.csv", index=False, encoding="utf-8")
    summary = dict(rows_before=n0, cols_before=c0, rows_after=len(out), cols_after=out.shape[1],
                   rows_lost_pct=round(100 * (n0 - len(out)) / n0, 4))
    pd.Series(summary).to_json(p["results"] / "summary.json", indent=1)
    log.info("Было %d x %d, стало %d x %d, потеряно %.4f %%", n0, c0, len(out), out.shape[1],
             summary["rows_lost_pct"])
    io.write_parquet(out, p["output"])
    return out


def run(mode="full", stage=None, input_path=None, output_path=None) -> None:
    p = paths_for(mode, input_path, output_path)
    for s in ([stage] if stage else STAGES):
        log.info("=== этап: %s (режим %s) ===", s, mode)
        {"serialize": stage_serialize, "type": stage_type, "clean": stage_clean}[s](p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("full", "debug"), default="full")
    ap.add_argument("--stage", choices=STAGES, help="выполнить один этап (по умолчанию все)")
    ap.add_argument("--input", help="исходный CSV (по умолчанию data/raw/branded_food.csv или срез)")
    ap.add_argument("--output", help="файл результата очистки (по умолчанию data/processed/clean.parquet)")
    a = ap.parse_args(argv)
    io.setup_logging("pipeline")
    try:
        if a.mode == "debug" and not a.input and not cfg.DEBUG_SLICE.exists():
            io.make_slice()
        run(a.mode, a.stage, a.input, a.output)
    except io.DataError as e:
        log.error("%s", e)
        return 1
    except FileNotFoundError as e:
        log.error("Файл не найден: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
