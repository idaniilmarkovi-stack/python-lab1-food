"""Функции очистки. Каждая принимает и возвращает таблицу и пишет в `stats`
число обработанных / изменённых / удалённых строк. Все функции идемпотентны."""
import html
import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as cfg
from .io_utils import apply_dates

log = logging.getLogger(__name__)

TEXT_COLS = ["brand_owner", "brand_name", "subbrand_name", "ingredients",
             "not_a_significant_source_of", "household_serving_fulltext",
             "branded_food_category", "package_weight", "short_description",
             "material_code", "preparation_state_code", "trade_channel",
             "serving_size_unit", "data_source", "market_country"]
_TAG = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")


class Stats(list):
    """Журнал этапов: список словарей {step, processed, changed, removed}."""

    def add(self, step: str, processed: int, changed: int = 0, removed: int = 0, note: str = ""):
        rec = dict(step=step, processed=int(processed), changed=int(changed),
                   removed=int(removed), note=note)
        self.append(rec)
        log.info("%-34s обработано=%d изменено=%d удалено=%d %s",
                 step, processed, changed, removed, note)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self)


# ------------------------------------------------------------------ помощники
def _map_values(s: pd.Series, func) -> pd.Series:
    """Применяет func к значениям; для category - только к словарю категорий
    (быстро: десятки тысяч вызовов вместо миллионов), объединяя совпавшие."""
    if isinstance(s.dtype, pd.CategoricalDtype):
        cats = list(s.cat.categories)
        mapped = [func(c) for c in cats]
        mapped = [None if (m is None or (isinstance(m, float) and np.isnan(m))) else m for m in mapped]
        new_cats = list(dict.fromkeys(m for m in mapped if m is not None))
        pos = {c: i for i, c in enumerate(new_cats)}
        cmap = np.array([pos[m] if m is not None else -1 for m in mapped] or [-1], dtype="int64")
        codes = s.cat.codes.to_numpy().astype("int64")
        new = np.where(codes >= 0, cmap[np.clip(codes, 0, None)], -1)
        return pd.Series(pd.Categorical.from_codes(new, categories=new_cats),
                         index=s.index, name=s.name)
    out = s.astype(object).map(lambda v: None if v is None or v is pd.NA or (isinstance(v, float) and np.isnan(v)) else func(v))
    return out.astype(cfg.STR)


def _count_changed(before: pd.Series, after: pd.Series) -> int:
    b = before.astype(object).where(before.notna(), None)
    a = after.astype(object).where(after.notna(), None)
    return int((b.ne(a) & ~(b.isna() & a.isna())).sum())


def _collapse(v: str) -> str:
    return _SPACES.sub(" ", v).strip()


# --------------------------------------------------------------------- этапы
def join_food(df: pd.DataFrame, stats: Stats, food_path: Path = cfg.RAW_FOOD) -> pd.DataFrame:
    """Левое соединение с food.csv по fdc_id (добавляет description, publication_date).
    Перед соединением проверяются ключ справочника и типы; после - число строк."""
    n0 = len(df)
    if "description" in df.columns:                      # идемпотентность
        stats.add("join_food", n0, note="уже соединено")
        return df
    if not Path(food_path).exists():
        stats.add("join_food", n0, note=f"пропущено: нет {Path(food_path).name}")
        return df
    parts = []
    for ch in pd.read_csv(food_path, usecols=["fdc_id", "data_type", "description",
                                              "publication_date"],
                          dtype={"fdc_id": "int32", "data_type": "string",
                                 "description": "string", "publication_date": "string"},
                          chunksize=500_000, keep_default_na=False, na_values=[""]):
        parts.append(ch[ch["data_type"] == "branded_food"].drop(columns="data_type"))
    food = pd.concat(parts, ignore_index=True)
    if food["fdc_id"].duplicated().any():
        raise ValueError("fdc_id в food.csv не уникален: соединение размножит строки")
    food["description"] = food["description"].astype(cfg.STR)
    food["publication_date"] = pd.to_datetime(food["publication_date"], format=cfg.DATE_FORMAT,
                                              errors="coerce")
    res = df.merge(food, on="fdc_id", how="left", validate="one_to_one")
    unmatched = int(res["description"].isna().sum())
    if len(res) != n0:
        raise ValueError(f"Соединение изменило число строк: {n0} -> {len(res)}")
    stats.add("join_food", n0, changed=n0 - unmatched,
              note=f"строк до/после: {n0}/{len(res)}; без пары в food.csv: {unmatched} "
                   f"({100 * unmatched / max(n0, 1):.2f} %)")
    return res


def normalize_column_names(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    new = [re.sub(r"_+", "_", re.sub(r"[^0-9a-z]+", "_", c.strip().lower())).strip("_")
           for c in df.columns]
    changed = sum(a != b for a, b in zip(df.columns, new))
    df = df.copy()
    df.columns = new
    stats.add("normalize_column_names", len(df), changed=0, note=f"переименовано столбцов: {changed}")
    return df


def cast_types(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """Явное приведение к схеме из config.SCHEMA (даты - с обработкой ошибок разбора)."""
    df = apply_dates(df.copy())
    for c, t in cfg.SCHEMA.items():
        if c not in df.columns or t == "datetime":
            continue
        if t == "category" and not isinstance(df[c].dtype, pd.CategoricalDtype):
            df[c] = df[c].astype("category")
        elif t not in ("category",) and str(df[c].dtype) != str(t):
            df[c] = df[c].astype(t)
    stats.add("cast_types", len(df), note="типы приведены к config.SCHEMA")
    return df


def replace_hidden_missing(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """«n/a», «none», «nan», пустые после strip, serving_size <= 0 и даты до 1990 г. -> NA."""
    df = df.copy()
    total_cells = 0
    detail = {}
    for c in TEXT_COLS:
        if c not in df.columns:
            continue
        bad = cfg.PLACEHOLDERS | cfg.EXTRA_PLACEHOLDERS.get(c, set())
        before_na = int(df[c].isna().sum())
        df[c] = _map_values(df[c], lambda v, bad=bad: None if (_collapse(str(v)) == "" or
                                                             _collapse(str(v)).lower() in bad) else v)
        n = int(df[c].isna().sum()) - before_na
        if n:
            detail[c] = n
            total_cells += n
    if "serving_size" in df.columns:
        m = df["serving_size"].le(0).fillna(False)
        if m.any():
            detail["serving_size<=0"] = int(m.sum())
            total_cells += int(m.sum())
            df.loc[m, "serving_size"] = np.float32("nan")
    lo = pd.Timestamp(cfg.MIN_VALID_DATE)
    for c in cfg.DATE_COLS:
        if c in df.columns:
            m = df[c].lt(lo).fillna(False)
            if m.any():
                detail[f"{c}<{cfg.MIN_VALID_DATE}"] = int(m.sum())
                total_cells += int(m.sum())
                df.loc[m, c] = pd.NaT
    stats.add("replace_hidden_missing", len(df), changed=total_cells,
              note=f"ячеек заменено на NA: {json.dumps(detail, ensure_ascii=False)}")
    return df


def analyze_missing(df: pd.DataFrame, stats: Stats, out_dir: Path | None = None) -> pd.DataFrame:
    """Доля пропусков по столбцам и по группам data_source: не сосредоточены ли пропуски?"""
    share = df.isna().mean().mul(100).round(3).rename("missing_pct")
    key = ["ingredients", "serving_size", "brand_name", "branded_food_category",
           "household_serving_fulltext", "package_weight"]
    by_src = df.groupby("data_source", observed=True)[[c for c in key if c in df.columns]] \
               .agg(lambda x: round(float(x.isna().mean() * 100), 3))
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        share.to_csv(out_dir / "missing_by_column.csv", encoding="utf-8")
        by_src.to_csv(out_dir / "missing_by_data_source.csv", encoding="utf-8")
    stats.add("analyze_missing", len(df), note="пропуски не удаляются и не заполняются (см. отчёт)")
    return df


def add_missing_flags(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """Флаги пропусков вместо заполнения медианой/модой, которое выдумало бы данные."""
    df = df.copy()
    flags = {"ingredients_missing": "ingredients", "serving_size_missing": "serving_size",
             "brand_name_missing": "brand_name", "category_missing": "branded_food_category"}
    for f, c in flags.items():
        df[f] = df[c].isna()
    stats.add("add_missing_flags", len(df), changed=int(df[list(flags)].any(axis=1).sum()),
              note="добавлено флагов: %d" % len(flags))
    return df


def normalize_gtin(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """GTIN: пробелы убираются; цифровые коды до 14 знаков дополняются нулями слева
    (UPC-12 и GTIN-14 одного товара получают одинаковый ключ)."""
    df = df.copy()
    s = df["gtin_upc"].astype("string").str.strip()
    digits = s.str.fullmatch(r"\d{1,14}").fillna(False)
    new = s.where(~digits, s.str.zfill(14))
    changed = _count_changed(df["gtin_upc"], new)
    df["gtin_upc"] = new.astype(cfg.STR)
    df["gtin_format_ok"] = df["gtin_upc"].str.fullmatch(r"\d{14}").fillna(False).astype(bool)
    stats.add("normalize_gtin", len(df), changed=changed,
              note=f"некорректных кодов: {int((~df['gtin_format_ok']).sum())}")
    return df


def drop_duplicates(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """Полные повторы и повторы fdc_id удаляются (остаётся последняя по modified_date).
    Один и тот же gtin_upc встречается в нескольких записях (разные fdc_id, даты, часто и владельцы),
    поэтому он не ключ товара: по умолчанию помечается последняя по дате запись (is_latest_version)."""
    n0 = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    n_full = n0 - len(df)
    df = df.sort_values(["fdc_id", "modified_date"], kind="stable")
    df = df.drop_duplicates("fdc_id", keep="last").reset_index(drop=True)
    n_key = n0 - n_full - len(df)
    order = df[["gtin_upc", "modified_date", "fdc_id"]].sort_values(
        ["modified_date", "fdc_id"], na_position="first", kind="stable")
    is_last = ~order.duplicated("gtin_upc", keep="last")
    df["is_latest_version"] = is_last.reindex(df.index).to_numpy()
    n_old = int((~df["is_latest_version"]).sum())
    removed_gtin = 0
    if cfg.GTIN_DUP_POLICY == "drop":
        removed_gtin = n_old
        df = df[df["is_latest_version"]].reset_index(drop=True)
    stats.add("drop_duplicates", n0, changed=0 if cfg.GTIN_DUP_POLICY == "drop" else n_old,
              removed=n_full + n_key + removed_gtin,
              note=f"полных повторов: {n_full}; повторов fdc_id: {n_key}; "
                   f"прежних версий gtin: {n_old} ({cfg.GTIN_DUP_POLICY})")
    return df


def _canonical_case(s: pd.Series) -> pd.Series:
    """Варианты, отличающиеся только регистром, заменяются самым частым написанием."""
    if not isinstance(s.dtype, pd.CategoricalDtype):
        return s
    counts = s.value_counts()
    best: dict[str, str] = {}
    for cat, n in counts.items():
        k = str(cat).casefold()
        if k not in best or n > counts[best[k]]:
            best[k] = cat
    return _map_values(s, lambda v: best[str(v).casefold()])


def normalize_categories(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """Краевые пробелы, двойные пробелы, регистр, словарь единиц, редкие категории."""
    df = df.copy()
    changed = {}
    cat_cols = ["brand_owner", "brand_name", "subbrand_name", "branded_food_category",
                "household_serving_fulltext", "preparation_state_code", "package_weight",
                "short_description", "data_source", "market_country"]
    for c in cat_cols:
        old = df[c]
        new = _map_values(old, _collapse)
        if c in ("brand_owner", "brand_name", "subbrand_name"):
            new = _canonical_case(new)
        n = _count_changed(old, new)
        if n:
            changed[c] = n
        df[c] = new
    old = df["serving_size_unit"]
    df["serving_size_unit"] = _map_values(old, lambda v: cfg.UNIT_MAP.get(_collapse(v).lower(), _collapse(v)))
    n = _count_changed(old, df["serving_size_unit"])
    if n:
        changed["serving_size_unit"] = n
    vc = df["branded_food_category"].value_counts()
    rare = set(vc[vc < cfg.RARE_CATEGORY_MIN].index)
    df["branded_food_category_grp"] = _map_values(
        df["branded_food_category"], lambda v: "Other" if v in rare else v)
    stats.add("normalize_categories", len(df), changed=sum(changed.values()),
              note=f"{json.dumps(changed, ensure_ascii=False)}; редких категорий (<{cfg.RARE_CATEGORY_MIN}): "
                   f"{len(rare)} -> группа Other")
    return df


def parse_trade_channel(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """trade_channel хранит JSON-список (нарушение 1НФ). Значения дедуплицируются,
    сортируются и записываются как 'A|B'; в БД превращаются в связь «многие ко многим»."""
    def norm(v: str):
        if v.startswith("["):
            try:
                items = json.loads(v)
            except json.JSONDecodeError:
                return v
            items = sorted({str(i).strip() for i in items if str(i).strip()})
            return "|".join(items) if items else None
        return v

    old = df["trade_channel"]
    df = df.copy()
    df["trade_channel"] = _map_values(old, norm)
    stats.add("parse_trade_channel", len(df), changed=_count_changed(old, df["trade_channel"]),
              note="JSON-список -> 'A|B' без повторов")
    return df


def clean_text(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """Свободный текст: удаление HTML-разметки, нормализация пробелов, пустые -> NA."""
    df = df.copy()
    changed = {}
    for c in ("ingredients", "not_a_significant_source_of"):
        old = df[c]
        df[c] = _map_values(old, lambda v: (_collapse(html.unescape(_TAG.sub(" ", v))) or None))
        changed[c] = _count_changed(old, df[c])
    stats.add("clean_text", len(df), changed=sum(changed.values()),
              note=json.dumps(changed, ensure_ascii=False))
    return df


_RE_G = re.compile(r"(\d+(?:\.\d+)?)\s*g\b", re.I)
_RE_ML = re.compile(r"(\d+(?:\.\d+)?)\s*ml\b", re.I)


def _first_number(rx: re.Pattern):
    def f(v: str):
        m = rx.search(v)
        return float(m.group(1)) if m else np.nan
    return f


def _cat_to_float(s: pd.Series, func) -> np.ndarray:
    cats = s.cat.categories
    vals = np.array([func(c) for c in cats] + [np.nan], dtype="float64")
    return vals[s.cat.codes.to_numpy()]      # код -1 -> последний элемент (NaN)


def normalize_units(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """Единая шкала порции: serving_size_std в г или мл (мг и мкг пересчитываются в г,
    IU не переводятся); package_weight -> package_weight_g / package_weight_ml.
    Исходные serving_size и package_weight сохраняются."""
    df = df.copy()
    u = df["serving_size_unit"].astype(object)
    factor = u.map(lambda x: cfg.UNIT_TO_BASE.get(x, (None, np.nan))[1]).astype("float64")
    base = u.map(lambda x: cfg.UNIT_TO_BASE.get(x, (None, np.nan))[0])
    df["serving_size_std"] = (df["serving_size"].astype("float64") * factor).astype("float32")
    df["serving_unit_std"] = base.astype("category")
    pw = df["package_weight"]
    df["package_weight_g"] = _cat_to_float(pw, _first_number(_RE_G)).astype("float32")
    df["package_weight_ml"] = _cat_to_float(pw, _first_number(_RE_ML)).astype("float32")
    # вес или объём упаковки <= 0 (например «0 g») - скрытый пропуск, а не измерение
    zero_w = int((df["package_weight_g"] <= 0).sum() + (df["package_weight_ml"] <= 0).sum())
    for c in ("package_weight_g", "package_weight_ml"):
        df.loc[df[c] <= 0, c] = np.float32("nan")
    # аналогично для порции в единой шкале (страховка: после пересчёта единиц)
    zero_s = int((df["serving_size_std"] <= 0).sum())
    df.loc[df["serving_size_std"] <= 0, "serving_size_std"] = np.float32("nan")
    conv = int((df["serving_unit_std"].notna() & df["serving_unit_std"].ne(df["serving_size_unit"].astype(object))).sum())
    stats.add("normalize_units", len(df), changed=conv,
              note=f"serving_size_std задан у {int(df['serving_size_std'].notna().sum())} строк; "
                   f"package_weight_g у {int(df['package_weight_g'].notna().sum())}, "
                   f"package_weight_ml у {int(df['package_weight_ml'].notna().sum())}; "
                   f"нулевых значений заменено на NA: вес/объём {zero_w}, порция {zero_s}")
    return df


def check_domain(df: pd.DataFrame, stats: Stats, today: pd.Timestamp | None = None) -> pd.DataFrame:
    """Предметные ограничения: даты не в будущем, discontinued_date не раньше available_date."""
    df = df.copy()
    today = today or pd.Timestamp.today().normalize()
    fut = {}
    for c in cfg.DATE_COLS + (["publication_date"] if "publication_date" in df.columns else []):
        m = df[c].gt(today).fillna(False)
        if m.any():
            fut[c] = int(m.sum())
            df.loc[m, c] = pd.NaT
    bad = (df["discontinued_date"] < df["available_date"]).fillna(False)
    df["date_order_violation"] = bad.astype(bool)
    neg = int((df["serving_size_std"] < 0).sum())
    stats.add("check_domain", len(df), changed=sum(fut.values()) + int(bad.sum()),
              note=f"даты в будущем->NaT: {json.dumps(fut)}; discontinued<available: {int(bad.sum())}; "
                   f"отрицательных порций: {neg}")
    return df


def flag_outliers(df: pd.DataFrame, stats: Stats, out_dir: Path | None = None) -> pd.DataFrame:
    """Выбросы serving_size_std двумя способами (IQR и MAD) отдельно для г и мл.
    Строки не удаляются: ставятся флаги; сверка способов пишется в outliers.json."""
    df = df.copy()
    x = df["serving_size_std"].astype("float64")
    grp = df["serving_unit_std"].astype(object)
    iqr = pd.Series(False, index=df.index)
    mad = pd.Series(False, index=df.index)
    bounds = {}
    for g in ("g", "ml"):
        m = grp.eq(g) & x.notna()
        v = x[m]
        if v.empty:
            continue
        q1, q3 = v.quantile([0.25, 0.75])
        lo, hi = q1 - cfg.IQR_K * (q3 - q1), q3 + cfg.IQR_K * (q3 - q1)
        med = v.median()
        mad_v = (v - med).abs().median()
        z = 0.6745 * (v - med).abs() / mad_v if mad_v > 0 else pd.Series(0.0, index=v.index)
        iqr[m] = (v < lo) | (v > hi)
        mad[m] = z > cfg.MAD_Z
        bounds[g] = dict(q1=float(q1), q3=float(q3), iqr_low=float(lo), iqr_high=float(hi),
                         median=float(med), mad=float(mad_v))
    df["serving_size_outlier_iqr"] = iqr.astype(bool)
    df["serving_size_outlier_mad"] = mad.astype(bool)
    df["serving_size_implausible"] = (x > cfg.SERVING_IMPLAUSIBLE).fillna(False).astype(bool)
    res = dict(rows=len(df), iqr=int(iqr.sum()), mad=int(mad.sum()),
               both=int((iqr & mad).sum()), either=int((iqr | mad).sum()),
               implausible=int(df["serving_size_implausible"].sum()),
               max_value=float(x.max()) if x.notna().any() else None, bounds=bounds)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "outliers.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    stats.add("flag_outliers", len(df), changed=res["either"],
              note=f"IQR={res['iqr']}, MAD={res['mad']}, оба={res['both']}, "
                   f"неправдоподобных (> {cfg.SERVING_IMPLAUSIBLE})={res['implausible']}")
    return df


def finalize(df: pd.DataFrame, stats: Stats) -> pd.DataFrame:
    """Итог: порядок строк по fdc_id, сквозной индекс, стабильный порядок столбцов."""
    df = df.sort_values("fdc_id", kind="stable").reset_index(drop=True)
    first = [c for c in cfg.SCHEMA if c in df.columns]
    df = df[first + [c for c in df.columns if c not in first]]
    stats.add("finalize", len(df), note=f"столбцов: {df.shape[1]}")
    return df


def clean_all(df: pd.DataFrame, stats: Stats | None = None, out_dir: Path | None = None,
              food_path: Path = cfg.RAW_FOOD) -> pd.DataFrame:
    """Полный конвейер очистки в рекомендованном порядке."""
    stats = stats if stats is not None else Stats()
    df = join_food(df, stats, food_path)
    df = normalize_column_names(df, stats)
    df = cast_types(df, stats)
    df = replace_hidden_missing(df, stats)
    df = analyze_missing(df, stats, out_dir)
    df = normalize_gtin(df, stats)
    df = drop_duplicates(df, stats)
    df = normalize_categories(df, stats)
    df = parse_trade_channel(df, stats)
    df = clean_text(df, stats)
    df = add_missing_flags(df, stats)     # после всех правок значений - иначе не идемпотентно
    df = normalize_units(df, stats)
    df = check_domain(df, stats)
    df = flag_outliers(df, stats, out_dir)
    return finalize(df, stats)
