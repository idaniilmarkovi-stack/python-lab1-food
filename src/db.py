"""Схема в третьей нормальной форме и загрузка очищенных данных в SQLite и PostgreSQL.

    python -m src.db sqlite                 # загрузка в data/processed/dataset.db + замеры
    python -m src.db postgres               # PostgreSQL (реквизиты в .env): INSERT и COPY
    python -m src.db all --input data/processed/clean_debug.parquet --limit 50000
Результаты замеров: results/db_sqlite.json, results/db_postgres.json.
"""
import argparse
import io as _io
import json
import logging
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as cfg
from . import io_utils as io

log = logging.getLogger("db")

# ---------------------------------------------------------------- схема (3НФ)
# Справочники: (таблица, первичный ключ, столбец имени, столбец DataFrame, тип имени)
REFS = [
    ("data_sources", "data_source_id", "code", "data_source", "VARCHAR(16)"),
    ("countries", "country_id", "name", "market_country", "VARCHAR(64)"),
    ("serving_units", "unit_id", "code", "serving_size_unit", "VARCHAR(8)"),
    ("prep_states", "prep_state_id", "code", "preparation_state_code", "VARCHAR(32)"),
    ("brand_owners", "brand_owner_id", "name", "brand_owner", "VARCHAR(255)"),
    ("brands", "brand_id", "name", "brand_name", "VARCHAR(255)"),
    ("subbrands", "subbrand_id", "name", "subbrand_name", "VARCHAR(255)"),
    ("food_categories", "category_id", "name", "branded_food_category", "VARCHAR(1024)"),
]
# столбец продукта: fk-столбец -> (таблица справочника)
FK = {r[3]: (r[0], r[1]) for r in REFS}
PRODUCT_COLS = ["fdc_id", "gtin_upc", "description", "brand_owner_id", "brand_id", "subbrand_id",
                "ingredients", "not_a_significant_source_of", "serving_size", "unit_id",
                "serving_size_std", "household_serving_fulltext", "category_id", "data_source_id",
                "package_weight", "package_weight_g", "package_weight_ml", "modified_date",
                "available_date", "publication_date", "country_id", "discontinued_date",
                "prep_state_id", "short_description", "material_code"]


def ddl() -> str:
    parts = []
    for t, pk, nm, _, typ in REFS:
        parts.append(f"CREATE TABLE {t} (\n  {pk} INTEGER PRIMARY KEY,\n  {nm} {typ} NOT NULL UNIQUE\n);")
    parts.append("""CREATE TABLE trade_channels (
  channel_id INTEGER PRIMARY KEY,
  name VARCHAR(64) NOT NULL UNIQUE
);""")
    parts.append("""CREATE TABLE branded_foods (
  fdc_id                      INTEGER PRIMARY KEY,
  gtin_upc                    VARCHAR(20) NOT NULL,
  description                 TEXT,
  brand_owner_id              INTEGER REFERENCES brand_owners(brand_owner_id),
  brand_id                    INTEGER REFERENCES brands(brand_id),
  subbrand_id                 INTEGER REFERENCES subbrands(subbrand_id),
  ingredients                 TEXT,
  not_a_significant_source_of TEXT,
  serving_size                REAL CHECK (serving_size IS NULL OR serving_size > 0),
  unit_id                     INTEGER REFERENCES serving_units(unit_id),
  serving_size_std            REAL CHECK (serving_size_std IS NULL OR serving_size_std > 0),
  household_serving_fulltext  VARCHAR(255),
  category_id                 INTEGER REFERENCES food_categories(category_id),
  data_source_id              INTEGER NOT NULL REFERENCES data_sources(data_source_id),
  package_weight              VARCHAR(64),
  package_weight_g            REAL CHECK (package_weight_g IS NULL OR package_weight_g > 0),
  package_weight_ml           REAL CHECK (package_weight_ml IS NULL OR package_weight_ml > 0),
  modified_date               DATE,
  available_date              DATE,
  publication_date            DATE,
  country_id                  INTEGER REFERENCES countries(country_id),
  discontinued_date           DATE,
  prep_state_id               INTEGER REFERENCES prep_states(prep_state_id),
  short_description           VARCHAR(64),
  material_code               VARCHAR(32)
);""")
    parts.append("""CREATE TABLE food_trade_channels (
  fdc_id     INTEGER NOT NULL REFERENCES branded_foods(fdc_id),
  channel_id INTEGER NOT NULL REFERENCES trade_channels(channel_id),
  PRIMARY KEY (fdc_id, channel_id)
);""")
    return "\n\n".join(parts) + "\n"


DROP_ORDER = ["food_trade_channels", "branded_foods", "trade_channels"] + [r[0] for r in REFS]
INDEXES = ["CREATE INDEX idx_foods_modified_date ON branded_foods(modified_date)",
           "CREATE INDEX idx_foods_category ON branded_foods(category_id)"]
QUERIES = {
    "count": "SELECT COUNT(*) FROM branded_foods",
    "aggregate": ("SELECT c.name, COUNT(*), AVG(f.serving_size_std) FROM branded_foods f "
                  "JOIN food_categories c ON c.category_id = f.category_id GROUP BY c.name"),
    "date_range": ("SELECT COUNT(*) FROM branded_foods "
                   "WHERE modified_date >= '2019-01-01' AND modified_date < '2019-07-01'"),
}


def write_ddl() -> None:
    cfg.SQL_DIR.mkdir(exist_ok=True)
    for name in ("sqlite", "postgres"):
        (cfg.SQL_DIR / f"schema_{name}.sql").write_text(
            f"-- Схема в 3НФ ({name}); создаётся модулем src/db.py\n" + ddl(), encoding="utf-8")


# ------------------------------------------------------- подготовка таблиц
def _ids(series: pd.Series):
    """Справочник из уникальных значений и столбец внешних ключей (1..N, NA -> None)."""
    codes, uniq = pd.factorize(series.astype(object), sort=True)
    ref = pd.DataFrame({"id": np.arange(1, len(uniq) + 1, dtype="int32"), "name": list(uniq)})
    fk = pd.array(np.where(codes >= 0, codes + 1, 0), dtype="Int32")
    fk[codes < 0] = pd.NA
    return ref, fk


def build_frames(df: pd.DataFrame) -> dict:
    """Разбор широкой таблицы на справочники и основную таблицу с внешними ключами."""
    frames, prod = {}, pd.DataFrame(index=df.index)
    fkcols = {"data_source": "data_source_id", "market_country": "country_id",
              "serving_size_unit": "unit_id", "preparation_state_code": "prep_state_id",
              "brand_owner": "brand_owner_id", "brand_name": "brand_id",
              "subbrand_name": "subbrand_id", "branded_food_category": "category_id"}
    for t, pk, nm, col, _ in REFS:
        ref, fk = _ids(df[col])
        frames[t] = ref.rename(columns={"id": pk, "name": nm})
        prod[fkcols[col]] = fk
    for c in PRODUCT_COLS:
        if c in prod.columns:
            continue
        if c in df.columns:
            s = df[c]
        else:
            s = pd.Series(pd.NA, index=df.index, dtype="object")
        if c in ("modified_date", "available_date", "publication_date", "discontinued_date"):
            s = s.dt.strftime("%Y-%m-%d") if c in df.columns else s
        elif c in ("serving_size", "serving_size_std", "package_weight_g", "package_weight_ml"):
            s = s.astype("float64").round(4)
        elif c == "fdc_id":
            s = s.astype("int32")
        else:
            s = s.astype(object)
        prod[c] = s
    frames["branded_foods"] = prod[PRODUCT_COLS]
    # связь «многие ко многим»: канал продаж (1НФ - по одному значению в строке)
    tc = df[["fdc_id", "trade_channel"]].dropna()
    tc = tc.assign(name=tc["trade_channel"].astype(str).str.split("|")).explode("name")
    ch, fk = _ids(tc["name"])
    frames["trade_channels"] = ch.rename(columns={"id": "channel_id"})
    frames["food_trade_channels"] = pd.DataFrame({"fdc_id": tc["fdc_id"].to_numpy(),
                                                  "channel_id": fk.astype("int32")})
    return frames


def _py(frame: pd.DataFrame):
    """Список столбцов с питоновскими значениями (None вместо NA/NaN)."""
    cols = []
    for c in frame.columns:
        s = frame[c].astype(object)
        cols.append(s.where(frame[c].notna(), None).tolist())
    return cols


def _batches(frame: pd.DataFrame, n: int):
    for i in range(0, len(frame), n):
        yield list(zip(*_py(frame.iloc[i:i + n])))


def _order(frames: dict):
    return [r[0] for r in REFS] + ["trade_channels", "branded_foods", "food_trade_channels"]


# ------------------------------------------------------------------- SQLite
def load_sqlite(df: pd.DataFrame, path: Path, batch: int = 10_000) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    frames = build_frames(df)
    t0 = time.perf_counter()
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(ddl())
    for t in _order(frames):
        f = frames[t]
        sql = f"INSERT INTO {t} ({','.join(f.columns)}) VALUES ({','.join('?' * f.shape[1])})"
        for rows in _batches(f, batch):
            con.executemany(sql, rows)
    con.commit()
    load_s = time.perf_counter() - t0
    res = {"rows": {t: int(len(frames[t])) for t in _order(frames)},
           "load_batch_insert_s": round(load_s, 3), "batch_size": batch}
    con.execute("ANALYZE")
    res["fk_violations"] = len(con.execute("PRAGMA foreign_key_check").fetchall())
    res["integrity_check"] = con.execute("PRAGMA integrity_check").fetchone()[0]
    res.update(_query_bench(con, "sqlite"))
    con.close()
    res["size_mb"] = round(path.stat().st_size / 1e6, 3)
    res["sqlite_version"] = sqlite3.sqlite_version
    return res


# --------------------------------------------------------------- PostgreSQL
def pg_connect():
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(cfg.ROOT / ".env")
    try:
        return psycopg2.connect(host=os.getenv("PG_HOST", "localhost"), port=os.getenv("PG_PORT", "5432"),
                                dbname=os.getenv("PG_DB"), user=os.getenv("PG_USER"),
                                password=os.getenv("PG_PASSWORD"))
    except psycopg2.OperationalError as e:
        raise io.DataError(f"Не удалось подключиться к PostgreSQL (проверьте .env и службу): {e}") from e


def _pg_reset(con) -> None:
    with con.cursor() as cur:
        for t in DROP_ORDER:
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        cur.execute(ddl())
    con.commit()


def load_pg_insert(con, frames: dict, batch: int = 10_000) -> float:
    from psycopg2.extras import execute_values
    _pg_reset(con)
    t0 = time.perf_counter()
    with con.cursor() as cur:
        for t in _order(frames):
            f = frames[t]
            sql = f"INSERT INTO {t} ({','.join(f.columns)}) VALUES %s"
            for rows in _batches(f, batch):
                execute_values(cur, sql, rows, page_size=batch)
    con.commit()
    return time.perf_counter() - t0


def load_pg_copy(con, frames: dict, chunk: int = 100_000) -> float:
    _pg_reset(con)
    t0 = time.perf_counter()
    with con.cursor() as cur:
        for t in _order(frames):
            f = frames[t]
            sql = f"COPY {t} ({','.join(f.columns)}) FROM STDIN WITH (FORMAT csv, NULL '')"
            for i in range(0, len(f), chunk):
                buf = _io.StringIO()
                part = f.iloc[i:i + chunk].copy()
                for c in part.columns:                  # целые с пропусками - без «.0»
                    if str(part[c].dtype).startswith("Int"):
                        part[c] = part[c].astype("Int64")
                part.to_csv(buf, header=False, index=False, na_rep="")
                buf.seek(0)
                cur.copy_expert(sql, buf)
    con.commit()
    return time.perf_counter() - t0


def load_postgres(df: pd.DataFrame, batch: int = 10_000) -> dict:
    frames = build_frames(df)
    con = pg_connect()
    res = {"rows": {t: int(len(frames[t])) for t in _order(frames)}, "batch_size": batch}
    with con.cursor() as cur:
        cur.execute("SHOW server_version")
        res["pg_version"] = cur.fetchone()[0]
    res["load_batch_insert_s"] = round(load_pg_insert(con, frames, batch), 3)
    res["size_after_insert_mb"] = _pg_size(con)
    res["load_copy_s"] = round(load_pg_copy(con, frames), 3)
    with con.cursor() as cur:
        cur.execute("ANALYZE")
    con.commit()
    res["size_mb"] = _pg_size(con)
    res.update(_query_bench(con, "postgres"))
    con.close()
    return res


def _pg_size(con) -> float:
    with con.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        return round(cur.fetchone()[0] / 1e6, 3)


# ------------------------------------------------------------ контрольные запросы
def _time(run, repeats: int = 3) -> float:
    run()                                  # прогрев
    ts = []
    for _ in range(repeats):
        t = time.perf_counter()
        run()
        ts.append(time.perf_counter() - t)
    return statistics.median(ts)


def _query_bench(con, kind: str) -> dict:
    def run(sql):
        if kind == "sqlite":
            return lambda: con.execute(sql).fetchall()

        def f():
            with con.cursor() as cur:
                cur.execute(sql)
                cur.fetchall()
        return f

    def one(sql):
        if kind == "sqlite":
            return con.execute(sql).fetchall()
        with con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    out = {"queries": {}, "check": {}}
    out["check"]["count"] = one(QUERIES["count"])[0][0]
    out["check"]["date_range"] = one(QUERIES["date_range"])[0][0]
    agg = one(QUERIES["aggregate"])
    out["check"]["aggregate_groups"] = len(agg)
    out["queries"]["count_s"] = round(_time(run(QUERIES["count"])), 6)
    out["queries"]["aggregate_s"] = round(_time(run(QUERIES["aggregate"])), 6)
    out["queries"]["date_range_no_index_s"] = round(_time(run(QUERIES["date_range"])), 6)
    if kind == "sqlite":
        for ix in INDEXES:
            con.execute(ix)
        con.execute("ANALYZE")
        con.commit()
    else:
        with con.cursor() as cur:
            for ix in INDEXES:
                cur.execute(ix)
            cur.execute("ANALYZE")
        con.commit()
    out["queries"]["date_range_with_index_s"] = round(_time(run(QUERIES["date_range"])), 6)
    plan_sql = ("EXPLAIN QUERY PLAN " if kind == "sqlite" else "EXPLAIN ") + QUERIES["date_range"]
    out["plan_date_range"] = [str(r) for r in one(plan_sql)]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", choices=("sqlite", "postgres", "all", "ddl"))
    ap.add_argument("--input", default=str(cfg.CLEAN_PARQUET))
    ap.add_argument("--sqlite-path", default=os.getenv("SQLITE_PATH", str(cfg.SQLITE_DEFAULT)))
    ap.add_argument("--batch", type=int, default=10_000)
    ap.add_argument("--limit", type=int, help="взять только первые N строк (для отладки)")
    a = ap.parse_args(argv)
    io.setup_logging("db")
    write_ddl()
    if a.target == "ddl":
        return 0
    try:
        df = io.read_parquet(Path(a.input))
        if a.limit:
            df = df.iloc[:a.limit].reset_index(drop=True)
        cfg.RESULTS.mkdir(exist_ok=True)
        if a.target in ("sqlite", "all"):
            r = load_sqlite(df, Path(a.sqlite_path), a.batch)
            (cfg.RESULTS / "db_sqlite.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            log.info("SQLite: загрузка %.2f c, размер %.1f МБ", r["load_batch_insert_s"], r["size_mb"])
        if a.target in ("postgres", "all"):
            r = load_postgres(df, a.batch)
            (cfg.RESULTS / "db_postgres.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            log.info("PostgreSQL: INSERT %.2f c, COPY %.2f c, размер %.1f МБ",
                     r["load_batch_insert_s"], r["load_copy_s"], r["size_mb"])
    except io.DataError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
