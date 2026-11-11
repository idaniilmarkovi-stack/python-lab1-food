"""Замеры для отчёта (таблицы 6, 7, 8). Результаты пишутся в results/*.csv и *.json.

    python -m src.benchmarks env            # конфигурация машины и версии библиотек
    python -m src.benchmarks formats        # таблица 6: CSV / Pickle / Feather / Parquet
    python -m src.benchmarks schema         # таблица 7: до и после назначения типов
    python -m src.benchmarks polars         # таблица 8: pandas против polars
    python -m src.benchmarks all
Опция --mode debug выполняет те же замеры на отладочном срезе (для проверки работоспособности).
Правила замера: 1 прогрев + 3 повтора, фиксируется медиана; одна пара «библиотека-операция»
запускается в отдельном процессе; пиковая память - RSS процесса (опрос раз в 2 мс).
"""
import argparse
import json
import logging
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from importlib import metadata

import pandas as pd

from . import config as cfg
from . import io_utils as io

log = logging.getLogger("bench")
REPEATS, WARMUP = 3, 1
THREE_COLS = ["fdc_id", "branded_food_category", "serving_size"]
MB = 1e6


def median_time(fn, repeats: int = REPEATS, warmup: int = WARMUP) -> float:
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    return statistics.median(ts)


def _paths(mode: str):
    sfx = "_debug" if mode == "debug" else ""
    return dict(raw=cfg.DEBUG_SLICE if mode == "debug" else cfg.RAW_BRANDED,
                source=cfg.INTERIM / f"source{sfx}.parquet",
                typed=cfg.INTERIM / f"source_typed{sfx}.parquet",
                tmp=cfg.INTERIM / "bench_tmp", out=cfg.RESULTS / ("bench_debug" if mode == "debug" else "bench"))


# --------------------------------------------------------------------- среда
def _cpu_name() -> str:
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor).Name"],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
            if out:
                return out.splitlines()[0].strip()
        elif platform.system() == "Linux":
            for line in open("/proc/cpuinfo", encoding="utf-8"):
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        elif platform.system() == "Darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True).stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return platform.processor() or "не определён"


def env_info() -> dict:
    import psutil
    def ver(n):
        try:
            return metadata.version(n)
        except metadata.PackageNotFoundError:
            return "не установлен"
    info = dict(
        python=platform.python_version(), python_bits=platform.architecture()[0],
        system=platform.platform(), cpu=_cpu_name(),
        cores_physical=psutil.cpu_count(logical=False), threads=psutil.cpu_count(logical=True),
        ram_gib=round(psutil.virtual_memory().total / 2**30, 2),
        disk_free_gib=round(shutil.disk_usage(cfg.ROOT).free / 2**30, 1),
        libs={n: ver(n) for n in ("pandas", "polars", "pyarrow", "numpy", "SQLAlchemy", "psycopg2-binary")},
        date=time.strftime("%d.%m.%Y"))
    return info


# ------------------------------------------------------------------ таблица 6
def bench_formats(mode: str = "full") -> pd.DataFrame:
    p = _paths(mode)
    tmp = p["tmp"]
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    csv_size = p["raw"].stat().st_size
    log.info("formats: чтение исходного CSV (базовая линия)")
    t_csv_read = median_time(lambda: io.read_csv_naive(p["raw"]))
    df = io.read_csv_naive(p["raw"])
    rows = [dict(format="CSV (исходный)", size_mb=csv_size / MB, write_s=None, read_s=t_csv_read)]
    # запись CSV - в отдельный файл в tmp, raw не изменяется
    f = tmp / "copy.csv"
    t_w = median_time(lambda: df.to_csv(f, index=False))
    rows[0]["write_s"] = t_w
    f.unlink()
    specs = [
        ("Pickle", tmp / "d.pkl", lambda f: df.to_pickle(f), lambda f: pd.read_pickle(f)),
        ("Feather", tmp / "d.feather", lambda f: df.to_feather(f), lambda f: pd.read_feather(f)),
        ("Parquet (snappy)", tmp / "d.parquet", lambda f: df.to_parquet(f, compression="snappy", index=False),
         lambda f: pd.read_parquet(f)),
    ]
    for name, f, w, r in specs:
        log.info("formats: %s", name)
        t_w = median_time(lambda: w(f))
        t_r = median_time(lambda: r(f))
        back = r(f)
        ok = back.shape == df.shape
        rows.append(dict(format=name, size_mb=f.stat().st_size / MB, write_s=t_w, read_s=t_r, shape_ok=ok))
        del back
    f = tmp / "d.parquet"
    t3 = median_time(lambda: pd.read_parquet(f, columns=THREE_COLS))
    rows.append(dict(format="Parquet, 3 столбца", size_mb=f.stat().st_size / MB, write_s=None, read_s=t3))
    res = pd.DataFrame(rows)
    res["ratio_to_csv"] = res["size_mb"] * MB / csv_size
    res["n_rows"], res["n_cols"] = df.shape
    shutil.rmtree(tmp, ignore_errors=True)
    p["out"].mkdir(parents=True, exist_ok=True)
    res.to_csv(p["out"] / "formats.csv", index=False, encoding="utf-8")
    return res


# ------------------------------------------------------------------ таблица 7
def _profile(df: pd.DataFrame, label: str) -> pd.DataFrame:
    m = df.memory_usage(deep=True, index=False)
    return pd.DataFrame({"column": m.index, "dtype": [str(df[c].dtype) for c in m.index],
                         f"memory_mb_{label}": (m.values / MB).round(3)})


def bench_schema(mode: str = "full") -> pd.DataFrame:
    p = _paths(mode)
    p["out"].mkdir(parents=True, exist_ok=True)
    stats = {}
    for label, path in (("before", p["source"]), ("after", p["typed"])):
        io.check_input(path, (".parquet",))
        log.info("schema: %s (%s)", label, path.name)
        t_read = median_time(lambda: pd.read_parquet(path))
        df = pd.read_parquet(path)
        key = "branded_food_category"
        t_grp = median_time(lambda d=df: d.groupby(key, observed=True)["serving_size"].median())
        prof = _profile(df, label)
        prof.to_csv(p["out"] / f"memory_by_column_{label}.csv", index=False, encoding="utf-8")
        stats[label] = dict(size_mb=path.stat().st_size / MB, mem_mb=float(df.memory_usage(deep=True).sum() / MB),
                            read_s=t_read, group_s=t_grp,
                            n_category=int(sum(isinstance(t, pd.CategoricalDtype) for t in df.dtypes)))
        if label == "before":     # доля уникальных значений текстовых столбцов - критерий category
            txt = [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]
            pd.DataFrame({"column": txt, "unique_share": [df[c].nunique() / len(df) for c in txt],
                          "unique": [df[c].nunique() for c in txt]}).to_csv(
                p["out"] / "unique_share.csv", index=False, encoding="utf-8")
        del df
    rows = [("Объём файла на диске, МБ", "size_mb"), ("Объём в памяти, МБ", "mem_mb"),
            ("Время чтения, с", "read_s"), ("Время группировки, с", "group_s"),
            ("Число столбцов типа category", "n_category")]
    res = pd.DataFrame([dict(metric=n, before=stats["before"][k], after=stats["after"][k]) for n, k in rows])
    res["change_pct"] = (res["after"] / res["before"].where(res["before"] != 0) - 1) * 100
    res.to_csv(p["out"] / "schema.csv", index=False, encoding="utf-8")
    return res


# ------------------------------------------------------------------ таблица 8
OPS = ["read", "filter", "group", "sort", "join", "chain", "group_lazy_file"]
_DS_REF = {"data_source": ["LI", "GDSN", "NZGDSN"],
           "data_source_name": ["Label Insight", "GDSN (поставщики)", "GDSN Новая Зеландия"]}


def _worker(lib: str, op: str, path: str, cols: list[str]) -> dict:
    import psutil
    proc, peak, stop = psutil.Process(), [0], threading.Event()

    def poll():
        while not stop.is_set():
            peak[0] = max(peak[0], proc.memory_info().rss)
            time.sleep(0.002)
    threading.Thread(target=poll, daemon=True).start()
    check = {}
    if lib == "pandas":
        ref = pd.DataFrame(_DS_REF)
        if op == "read":
            fn = lambda: pd.read_parquet(path)  # noqa: E731
        else:
            df = pd.read_parquet(path)
            filt = lambda: df[(df["serving_size"] > 100) & (df["serving_size_unit"] == "g")]  # noqa: E731
            grp = lambda d: d.groupby("branded_food_category", observed=True)["serving_size"].median()  # noqa: E731
            fn = {
                "filter": filt,
                "group": lambda: grp(df),
                "sort": None,
                "join": lambda: df.assign(data_source=df["data_source"].astype(str)).merge(ref, on="data_source", how="left"),
                "chain": lambda: grp(filt()).sort_values(ascending=False),
                "group_lazy_file": lambda: grp(pd.read_parquet(path, columns=cols)),
            }[op]
            if op == "sort":
                g = grp(df)
                fn = lambda: g.sort_values(ascending=False)  # noqa: E731
    else:
        import polars as pl
        ref = pl.DataFrame(_DS_REF)
        if op == "read":
            fn = lambda: pl.read_parquet(path)  # noqa: E731
        else:
            df = pl.read_parquet(path)
            cond = (pl.col("serving_size") > 100) & (pl.col("serving_size_unit") == "g")
            agg = lambda lf: lf.group_by("branded_food_category").agg(pl.col("serving_size").median())  # noqa: E731
            fn = {
                "filter": lambda: df.filter(cond),
                "group": lambda: agg(df),
                "sort": None,
                "join": lambda: df.with_columns(pl.col("data_source").cast(pl.String)).join(ref, on="data_source", how="left"),
                "chain": lambda: agg(df.lazy().filter(cond)).sort("serving_size", descending=True).collect(),
                "group_lazy_file": lambda: agg(pl.scan_parquet(path).select(cols)).collect(),
            }[op]
            if op == "sort":
                g = agg(df)
                fn = lambda: g.sort("serving_size", descending=True)  # noqa: E731
    base = proc.memory_info().rss
    out = fn()
    n = len(out)
    check["rows"] = int(n)
    t = median_time(fn)
    if lib == "polars" and op == "group":      # ленивый режим той же группировки (по таблице в памяти)
        import polars as pl
        t_lazy = median_time(lambda: df.lazy().group_by("branded_food_category")
                             .agg(pl.col("serving_size").median()).collect())
        check["lazy_group_s"] = t_lazy
    stop.set()
    return dict(lib=lib, op=op, time_s=t, peak_rss_mb=peak[0] / MB, rss_before_op_mb=base / MB, **check)


def bench_polars(mode: str = "full") -> pd.DataFrame:
    p = _paths(mode)
    p["out"].mkdir(parents=True, exist_ok=True)
    io.check_input(p["typed"], (".parquet",))
    rows = []
    for op in OPS:
        for lib in ("pandas", "polars"):
            log.info("polars-bench: %s / %s", lib, op)
            r = subprocess.run([sys.executable, "-m", "src.benchmarks", "_worker", lib, op, str(p["typed"])],
                               capture_output=True, text=True, cwd=cfg.ROOT)
            if r.returncode != 0:
                log.error("worker %s/%s: %s", lib, op, r.stderr[-1500:])
                raise RuntimeError(f"Замер {lib}/{op} завершился ошибкой (см. журнал)")
            rows.append(json.loads(r.stdout.strip().splitlines()[-1]))
    long = pd.DataFrame(rows)
    wide = long.pivot(index="op", columns="lib", values=["time_s", "peak_rss_mb", "rows"])
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    wide = wide.reindex(OPS).reset_index()
    wide["speedup"] = wide["time_s_pandas"] / wide["time_s_polars"]
    lazy = long.loc[(long.lib == "polars") & (long.op == "group"), "lazy_group_s"]
    wide.attrs["lazy_group_s"] = float(lazy.iloc[0]) if len(lazy) else None
    wide.to_csv(p["out"] / "pandas_polars.csv", index=False, encoding="utf-8")
    (p["out"] / "polars_lazy_group.json").write_text(json.dumps({"lazy_group_s": wide.attrs["lazy_group_s"]}), encoding="utf-8")
    return wide


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("env", "formats", "schema", "polars", "all", "_worker"))
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--mode", choices=("full", "debug"), default="full")
    a = ap.parse_args(argv)
    if a.what == "_worker":                      # служебный вызов из bench_polars
        lib, op, path = a.rest
        cols = ["branded_food_category", "serving_size"]
        print(json.dumps(_worker(lib, op, path, cols)))
        return 0
    io.setup_logging("benchmarks")
    out = _paths(a.mode)["out"]
    out.mkdir(parents=True, exist_ok=True)
    try:
        if a.what in ("env", "all"):
            (out / "env.json").write_text(json.dumps(env_info(), ensure_ascii=False, indent=1), encoding="utf-8")
        if a.what in ("formats", "all"):
            print(bench_formats(a.mode).round(4).to_string())
        if a.what in ("schema", "all"):
            print(bench_schema(a.mode).round(4).to_string())
        if a.what in ("polars", "all"):
            print(bench_polars(a.mode).round(4).to_string())
    except io.DataError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
