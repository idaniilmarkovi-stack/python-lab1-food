# Лабораторная работа № 1. Обработка и очистка данных (USDA Branded Foods)

Воспроизводимый маршрут подготовки данных: CSV → Parquet → типизированный набор → очистка → SQLite и PostgreSQL.
Набор: `branded_food.csv` из [FoodData Central](https://fdc.nal.usda.gov/download-datasets) (1,98 млн строк × 21 столбец). Паспорт — в [DATASET.md](DATASET.md).

**Требуемая версия интерпретатора: Python 3.12.14.**

## Развёртывание
```
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -c "import sys; print(sys.version, sys.prefix != sys.base_prefix)"   # 3.12.14 ... True
pip install poetry
poetry config virtualenvs.in-project true
poetry install                      # состав берётся из poetry.lock
copy .env.example .env              # затем заполнить реквизиты PostgreSQL
```
Положите `branded_food.csv` (и при желании `food.csv`) в `data/raw/`. Каталог `data/` в git не попадает.

## Запуск
```
python -m src.pipeline --mode debug          # отладочный срез (200 тыс. строк)
python -m src.pipeline                       # полный набор: serialize -> type -> clean
python -m src.pipeline --stage clean         # один этап
python -m src.validation data/processed/clean.parquet                 # идемпотентность
python -m src.validation data/processed/clean.parquet data/processed/clean_notebook.parquet
python -m src.benchmarks all                 # замеры: форматы, типы, pandas/polars
python -m src.db sqlite                      # SQLite + замеры запросов и индекса
python -m src.db postgres                    # PostgreSQL: INSERT и COPY
python -m src.rebuild                        # очистить interim и processed, построить заново
```
Блокноты: `notebooks/01_overview.ipynb` (разведка), `02_cleaning.ipynb` (очистка по шагам; та же логика, что в `src/cleaning.py`),
`03_colab.ipynb` (Google Colab, самодостаточный).

## Структура
```
data/raw/        исходные файлы (только чтение)
data/interim/    source.parquet, source_typed.parquet, срез
data/processed/  clean.parquet, dataset.db
src/             config, io_utils, cleaning, pipeline, validation, db, benchmarks, rebuild
sql/             schema_sqlite.sql, schema_postgres.sql (DDL, 3НФ)
results/         таблицы замеров, журнал очистки
docs/            ER-диаграмма, профиль набора
```
Движение данных: `raw → interim/source.parquet → interim/source_typed.parquet → processed/clean.parquet → dataset.db / PostgreSQL`.
Обратной записи в `raw` нет.
