"""Генерирует блокноты notebooks/*.ipynb (без выводов; выводы появятся после запуска)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def md(t): return {"cell_type": "markdown", "metadata": {}, "source": t.strip("\n").splitlines(True)}
def code(t): return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                     "source": t.strip("\n").splitlines(True)}


def save(name, cells):
    nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                                       "language_info": {"name": "python"}}, "nbformat": 4, "nbformat_minor": 5}
    (ROOT / "notebooks" / name).write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")


SETUP = """import sys
from pathlib import Path
ROOT = Path.cwd().parent if Path.cwd().name == 'notebooks' else Path.cwd()
sys.path.insert(0, str(ROOT))"""

# ---------------------------------------------------------------- 01_overview
save("01_overview.ipynb", [
    md("# 01. Первичная разведка набора USDA Branded Food\nЦель: убедиться в окружении, оценить объём, типы, пропуски и уникальность, "
       "составить перечень дефектов (план очистки). Работа идёт на отладочном срезе (200 тыс. строк), "
       "полный набор обрабатывается скриптом `src.pipeline`."),
    md("## 1. Окружение\nПроверяем, что ядро блокнота запущено интерпретатором Python 3.12.14 из `.venv`."),
    code(SETUP + """
import platform
print(platform.python_version())
print(sys.executable)
print('виртуальное окружение активно:', sys.prefix != sys.base_prefix)"""),
    md("## 2. Файл и срез\nСрез формируется потоковым чтением (в память попадает одна строка за раз)."),
    code("""from src import config as cfg, io_utils as io
if not cfg.DEBUG_SLICE.exists():
    io.make_slice()
info = io.file_info(cfg.RAW_BRANDED)
print({k: v for k, v in info.items() if k != 'columns'})
info['columns']"""),
    md("## 3. Загрузка среза «как есть» (типы подбирает pandas)"),
    code("""import pandas as pd
from IPython.display import display
pd.set_option('display.max_columns', 50); pd.set_option('display.width', 200)
df = io.read_csv_naive(cfg.DEBUG_SLICE)
print(df.shape)
display(df.head(5))"""),
    md("## 4. Типы, память, пропуски, уникальность\nДоля уникальных значений — критерий перевода в `category`: "
       "меньше 50 % — категория; почти уникальные столбцы (идентификаторы, длинный текст) в категорию не переводятся."),
    code("""prof = pd.DataFrame({
    'dtype': df.dtypes.astype(str),
    'unique': df.nunique(),
    'unique_share': (df.nunique() / len(df)).round(4),
    'missing_pct': (df.isna().mean() * 100).round(2),
    'memory_mb': (df.memory_usage(deep=True, index=False) / 1e6).round(2)})
display(prof)
print('Всего в памяти, МБ:', round(df.memory_usage(deep=True).sum() / 1e6, 1))"""),
    md("## 5. Скрытые дефекты\nЗаглушки вместо пропусков, служебные символы, регистр, ведущие нули GTIN."),
    code("""placeholders = {'n/a', 'na', 'none', 'null', 'nan', '-', '--', 'unknown'}
rows = []
for c in df.select_dtypes(exclude='number').columns:
    s = df[c].dropna().astype(str)
    rows.append(dict(column=c,
                     placeholders=int(s.str.strip().str.lower().isin(placeholders).sum()),
                     edge_spaces=int((s != s.str.strip()).sum()),
                     double_spaces=int(s.str.contains('  ').sum()),
                     html_like=int(s.str.contains('<[^>]+>', regex=True).sum())))
display(pd.DataFrame(rows).set_index('column'))
print('gtin_upc прочитан как', df['gtin_upc'].dtype, '- ведущие нули утеряны:', df['gtin_upc'].head(3).tolist())
display(df['serving_size_unit'].value_counts())
display(df['serving_size'].describe())"""),
    md("## 6. Перечень дефектов (план очистки)\nСм. `DATASET.md`, раздел «Дефекты». Кратко: неверный тип `gtin_upc`; "
       "заглушки `n/a`, `none`, `nan`; пробелы и регистр в `brand_owner`; разные коды единиц (`g/GRM/GM`, `ml/MLT`); "
       "JSON-список в `trade_channel`; HTML в `ingredients`; нулевые и огромные значения `serving_size`; "
       "несколько версий одного GTIN."),
])

# ---------------------------------------------------------------- 02_cleaning
save("02_cleaning.ipynb", [
    md("# 02. Очистка данных по шагам\nБлокнот вызывает общие функции из `src/cleaning.py` по одной и показывает результат каждого шага. "
       "Тот же код запускается из командной строки: `python -m src.pipeline`. Исполняется целиком после перезапуска ядра."),
    code(SETUP + """
import logging
import pandas as pd
from IPython.display import display, Markdown
from src import config as cfg, io_utils as io, cleaning as cl, validation
logging.basicConfig(level=logging.WARNING)
pd.set_option('display.max_columns', 60); pd.set_option('display.width', 220); pd.set_option('display.max_colwidth', 80)
MODE = 'debug'          # 'debug' - срез 200 тыс. строк, 'full' - весь набор
sfx = '_debug' if MODE == 'debug' else ''
SRC = cfg.DEBUG_SLICE if MODE == 'debug' else cfg.RAW_BRANDED
OUT_DIR = cfg.RESULTS / ('debug_nb' if MODE == 'debug' else 'full_nb')
stats = cl.Stats()"""),
    md("## Исходная таблица с явной схемой типов\nЧтение с параметром `dtype` из `config.SCHEMA`; `keep_default_na=False` — заглушки вроде `n/a` не исчезают молча."),
    code("""if MODE == 'debug' and not SRC.exists():
    io.make_slice()
df = io.read_csv_typed(SRC)
n0, c0 = df.shape
print(n0, c0, round(df.memory_usage(deep=True).sum() / 1e6, 1), 'МБ')
display(df.dtypes.astype(str).value_counts())"""),
    md("## Шаг 1. Соединение с `food.csv` по `fdc_id`\nЛевое соединение 1:1 (ключ справочника проверен на уникальность). "
       "Контроль: число строк до и после и число строк без пары."),
    code("""df = cl.join_food(df, stats)
display(stats.frame().tail(1))"""),
    md("## Шаг 2. Имена столбцов и типы"),
    code("""df = cl.normalize_column_names(df, stats)
df = cl.cast_types(df, stats)
display(stats.frame().tail(2))"""),
    md("## Шаг 3. Скрытые пропуски\n`n/a`, `none`, `nan`, пустые после обрезки пробелов, порция ≤ 0, даты до 1990 г. → `NA`."),
    code("""before = df.isna().sum()
df = cl.replace_hidden_missing(df, stats)
display((df.isna().sum() - before).loc[lambda s: s > 0].to_frame('добавлено NA'))"""),
    md("## Шаг 4. Структура пропусков\nПропуски не удаляются и не заполняются модой или медианой: это выдумало бы данные. "
       "Проверяем, не сосредоточены ли они в одной группе (`data_source`)."),
    code("""df = cl.analyze_missing(df, stats, OUT_DIR)
display(pd.read_csv(OUT_DIR / 'missing_by_data_source.csv', index_col=0))"""),
    md("## Шаг 5. GTIN и повторы\nКоды дополняются нулями до 14 знаков; полные дубликаты и повторы `fdc_id` удаляются; "
       "прежние версии одного GTIN помечаются флагом `is_latest_version`."),
    code("""n = len(df)
df = cl.normalize_gtin(df, stats)
df = cl.drop_duplicates(df, stats)
print('строк было', n, 'стало', len(df))
display(df['is_latest_version'].value_counts())"""),
    md("## Шаг 6. Категории\nПробелы, регистр, словарь единиц (`GRM/GM→g`, `MLT→ml`, `MC→mcg`), редкие категории → группа `Other`."),
    code("""df = cl.normalize_categories(df, stats)
display(df['serving_size_unit'].value_counts(dropna=False))
display(df['branded_food_category_grp'].value_counts().head(8))"""),
    md("## Шаг 7. Каналы продаж и текст\n`trade_channel` — JSON-список (нарушение 1НФ) → `A|B` без повторов; из `ingredients` удаляется HTML, нормализуются пробелы."),
    code("""df = cl.parse_trade_channel(df, stats)
df = cl.clean_text(df, stats)
display(df['trade_channel'].dropna().value_counts().head(5))"""),
    md("## Шаг 8. Флаги пропусков и единая шкала\n`serving_size_std` — порция в граммах или миллилитрах; `package_weight` разбирается в `package_weight_g` / `package_weight_ml`."),
    code("""df = cl.add_missing_flags(df, stats)
df = cl.normalize_units(df, stats)
display(df[['serving_size', 'serving_size_unit', 'serving_size_std', 'serving_unit_std',
            'package_weight', 'package_weight_g', 'package_weight_ml']].dropna(subset=['package_weight']).head(6))"""),
    md("## Шаг 9. Предметные ограничения и выбросы\nДаты не в будущем; `discontinued_date` не раньше `available_date`. Выбросы порции — двумя способами (IQR и MAD), "
       "без удаления: ставятся флаги."),
    code("""df = cl.check_domain(df, stats)
df = cl.flag_outliers(df, stats, OUT_DIR)
import json; print(json.dumps(json.load(open(OUT_DIR / 'outliers.json')), indent=1)[:900])"""),
    md("## Итог: «было — стало»"),
    code("""df = cl.finalize(df, stats)
display(stats.frame())
print(f'Было {n0} x {c0}, стало {len(df)} x {df.shape[1]}; потеряно строк: {100 * (n0 - len(df)) / n0:.4f} %')"""),
    md("## Сохранение и сверка с командной строкой\nРезультат блокнота пишется в отдельный файл и сравнивается с результатом `python -m src.pipeline` построчно."),
    code("""out = cfg.PROCESSED / f'clean_notebook{sfx}.parquet'
io.write_parquet(df, out)
cli = cfg.PROCESSED / f'clean{sfx}.parquet'
if cli.exists():
    validation.compare(cli, out)
else:
    print('Сначала выполните: python -m src.pipeline', '--mode debug' if MODE == 'debug' else '')"""),
    md("## Идемпотентность\nПовторная очистка готовой таблицы ничего не меняет."),
    code("""again = cl.clean_all(df.copy(), cl.Stats(), None)
pd.testing.assert_frame_equal(df, again, check_exact=True)
print('идемпотентность подтверждена')"""),
])

# ---------------------------------------------------------------- 03_colab
save("03_colab.ipynb", [
    md("# 03. Google Colab: исследование отладочного среза\nБлокнот самодостаточен (не импортирует `src`) и запускается в Colab. "
       "Срез (`debug_200k.csv`) загружается с компьютера кнопкой в ячейке 4 либо с Google Диска."),
    md("## 1. Версия интерпретатора Colab\nВ курсе требуется 3.12.14; Colab задаёт версию сам, поэтому результат лишь проверяется, а не сдаётся из Colab."),
    code("""import sys
print(sys.version)
print('совпадает с 3.12.14:', sys.version.startswith('3.12.14'))"""),
    md("## 2. Установка библиотек\nКоманда с восклицательным знаком выполняется в оболочке сеанса."),
    code("""!pip install -q pandas polars
import pandas as pd, polars as pl
print('pandas', pd.__version__, '| polars', pl.__version__)"""),
    md("## 3. Загрузка среза\nВариант А — прямая загрузка файла. Вариант Б (закомментирован) — подключение Google Диска."),
    code("""from google.colab import files
uploaded = files.upload()          # выберите debug_200k.csv
path = next(iter(uploaded))
# from google.colab import drive; drive.mount('/content/drive'); path = '/content/drive/MyDrive/debug_200k.csv'
path"""),
    md("## 4. Чтение, размер, типы и доля пропусков"),
    code("""df = pd.read_csv(path, low_memory=False)
print('строк:', df.shape[0], 'столбцов:', df.shape[1])
info = pd.DataFrame({'тип': df.dtypes.astype(str), 'пропусков, %': (df.isna().mean() * 100).round(2)})
info"""),
    md("## 5. График: число записей по годам изменения (`modified_date`)"),
    code("""import matplotlib.pyplot as plt
years = pd.to_datetime(df['modified_date'], errors='coerce').dt.year.value_counts().sort_index()
ax = years.plot(kind='bar', figsize=(9, 4), color='#3a6ea5')
ax.set_xlabel('Год изменения записи'); ax.set_ylabel('Число записей'); ax.set_title('Записи Branded Food по годам изменения')
plt.tight_layout(); plt.show()"""),
    md("**Вывод.** Основная масса записей изменена в последние годы наблюдений: набор — «слепок» базы на дату выгрузки, а не равномерная история."),
])
print("notebooks ok")
