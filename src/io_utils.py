"""Чтение и запись файлов: CSV, Parquet, отладочный срез, сведения о файле."""
import csv
import hashlib
import logging
from pathlib import Path

import pandas as pd

from . import config as cfg

log = logging.getLogger(__name__)


class DataError(Exception):
    """Понятная пользователю ошибка входных данных."""


def setup_logging(name: str = "pipeline") -> None:
    cfg.LOGS.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, force=True,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(cfg.LOGS / f"{name}.log", encoding="utf-8"),
                  logging.StreamHandler()])


def check_input(path: Path, suffixes=(".csv", ".parquet")) -> Path:
    path = Path(path)
    if not path.exists():
        raise DataError(f"Файл не найден: {path}")
    if path.suffix.lower() not in suffixes:
        raise DataError(f"Неподдерживаемый формат {path.suffix!r}; ожидается {suffixes}")
    return path


def file_info(path: Path) -> dict:
    """Размер, кодировка (BOM), число полей в заголовке - без чтения файла целиком."""
    path = check_input(path, (".csv",))
    with open(path, "rb") as f:
        head = f.read(4096)
    enc = "utf-8-sig" if head.startswith(b"\xef\xbb\xbf") else "utf-8"
    with open(path, "r", encoding=enc, newline="") as f:
        header = next(csv.reader(f))
    return {"path": str(path), "bytes": path.stat().st_size, "encoding": enc,
            "n_columns": len(header), "columns": header}


def sha256(path: Path, block: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            h.update(chunk)
    return h.hexdigest()


def make_slice(src: Path = cfg.RAW_BRANDED, dst: Path = cfg.DEBUG_SLICE,
               n_rows: int = cfg.SLICE_ROWS) -> int:
    """Отладочный срез: потоковое чтение, в память попадает одна строка за раз."""
    check_input(src, (".csv",))
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "r", encoding="utf-8", newline="") as fi, \
            open(dst, "w", encoding="utf-8", newline="") as fo:
        rd, wr = csv.reader(fi), csv.writer(fo)
        wr.writerow(next(rd))
        n = 0
        for row in rd:
            wr.writerow(row)
            n += 1
            if n >= n_rows:
                break
    log.info("Срез записан: %s, строк: %d", dst, n)
    return n


def read_csv_naive(path: Path) -> pd.DataFrame:
    """Чтение «как есть»: типы подбирает pandas (так читают без схемы)."""
    check_input(path, (".csv",))
    return pd.read_csv(path, low_memory=False)


def apply_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Даты -> datetime; нераспознанные значения становятся NaT (число пишется в журнал)."""
    for c in cfg.DATE_COLS:
        if c in df.columns and not pd.api.types.is_datetime64_any_dtype(df[c]):
            raw = df[c].astype("string")
            parsed = pd.to_datetime(raw, format=cfg.DATE_FORMAT, errors="coerce")
            bad = int((parsed.isna() & raw.notna()).sum())
            if bad:
                log.warning("%s: нераспознано дат: %d", c, bad)
            df[c] = parsed
    return df


def read_csv_typed(path: Path) -> pd.DataFrame:
    """Чтение с явной схемой: неоптимальные типы не создаются вовсе.
    keep_default_na=False: пустой является пропуском, а «N/A», «none» читаются как текст -
    скрытые пропуски выявляются на этапе очистки явно, а не молча."""
    check_input(path, (".csv",))
    found = file_info(path)["columns"]
    if found != list(cfg.SCHEMA):
        raise DataError(f"Неожиданный набор столбцов в {path.name}: ожидается {list(cfg.SCHEMA)}, "
                        f"найдено {found}")
    dtypes = {c: (t if t != "datetime" else cfg.STR) for c, t in cfg.SCHEMA.items()}
    try:
        df = pd.read_csv(path, dtype=dtypes, keep_default_na=False, na_values=[""],
                         engine="c", encoding="utf-8", low_memory=False)
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as e:
        raise DataError(f"Повреждённый или неожиданный CSV ({path.name}): {e}") from e
    return apply_dates(df)


def write_parquet(df: pd.DataFrame, path: Path, compression: str = "snappy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, compression=compression, index=False)
    log.info("Записан %s (%.1f МБ)", path, path.stat().st_size / 1e6)
    return path


def read_parquet(path: Path, columns=None) -> pd.DataFrame:
    check_input(path, (".parquet",))
    return pd.read_parquet(path, columns=columns)
