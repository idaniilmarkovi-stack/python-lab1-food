"""Пути, параметры и схема данных проекта (единственное место, где они заданы)."""
from pathlib import Path

import pandas as pd

try:  # строки в формате Arrow, если установлен pyarrow
    import pyarrow  # noqa: F401
    STR = pd.StringDtype("pyarrow")
except ImportError:  # pragma: no cover
    STR = pd.StringDtype()

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW, INTERIM, PROCESSED = DATA / "raw", DATA / "interim", DATA / "processed"
LOGS, RESULTS, SQL_DIR, DOCS = ROOT / "logs", ROOT / "results", ROOT / "sql", ROOT / "docs"

RAW_BRANDED = RAW / "branded_food.csv"
RAW_FOOD = RAW / "food.csv"          # необязательный: описание продукта (соединение по fdc_id)
SLICE_ROWS = 200_000
DEBUG_SLICE = INTERIM / "debug_200k.csv"

# имена результатов этапов (каждый этап пишет в свой файл)
SOURCE_PARQUET = INTERIM / "source.parquet"            # сериализация «как есть»
TYPED_PARQUET = INTERIM / "source_typed.parquet"       # после назначения схемы
CLEAN_PARQUET = PROCESSED / "clean.parquet"            # итог очистки
SQLITE_DEFAULT = PROCESSED / "dataset.db"

# ---------------------------------------------------------------- схема типов
# Границы типов выбраны по предметной области, а не по текущей выборке:
#  fdc_id    - идентификатор USDA, сейчас ~2,7 млн; int32 (до 2,1 млрд) с запасом;
#  serving_size - порция в г/мл с 1-3 значащими цифрами; float32 (7 цифр) достаточно;
#  gtin_upc  - идентификатор: строка (иначе теряются ведущие нули).
SCHEMA = {
    "fdc_id": "int32",
    "brand_owner": "category",
    "brand_name": "category",
    "subbrand_name": "category",
    "gtin_upc": STR,
    "ingredients": STR,
    "not_a_significant_source_of": STR,
    "serving_size": "float32",
    "serving_size_unit": "category",
    "household_serving_fulltext": "category",
    "branded_food_category": "category",
    "data_source": "category",
    "package_weight": "category",
    "modified_date": "datetime",
    "available_date": "datetime",
    "market_country": "category",
    "discontinued_date": "datetime",
    "preparation_state_code": "category",
    "trade_channel": "category",
    "short_description": "category",
    "material_code": "category",
}
DATE_COLS = [c for c, t in SCHEMA.items() if t == "datetime"]
DATE_FORMAT = "%Y-%m-%d"
# Столбцы, НЕ переводимые в category: почти уникальные (идентификаторы, свободный
# текст) - gtin_upc (23 % уникальных, это идентификатор), ingredients (20 % уникальных,
# длинный текст), not_a_significant_source_of (длинный текст).

# ------------------------------------------------------------ параметры очистки
PLACEHOLDERS = {"n/a", "na", "none", "null", "nan", "-", "--", "unknown",
                "not available", "n.a.", "нет данных"}
EXTRA_PLACEHOLDERS = {"household_serving_fulltext": {"0"}}
MIN_VALID_DATE = "1990-01-01"      # более ранние даты - заменитель пропуска
RARE_CATEGORY_MIN = 100            # категории реже - объединяются в группу "Other"
SERVING_IMPLAUSIBLE = 5_000        # порция более 5 кг/л - предметно неправдоподобна
IQR_K, MAD_Z = 1.5, 3.5
UNIT_MAP = {"g": "g", "grm": "g", "gm": "g", "ml": "ml", "mlt": "ml",
            "mg": "mg", "mc": "mcg", "mcg": "mcg", "iu": "IU"}
UNIT_TO_BASE = {"g": ("g", 1.0), "mg": ("g", 1e-3), "mcg": ("g", 1e-6), "ml": ("ml", 1.0)}
# Повторы по ключу gtin_upc: "flag" - пометить последнюю версию, "drop" - оставить только её
GTIN_DUP_POLICY = "flag"
