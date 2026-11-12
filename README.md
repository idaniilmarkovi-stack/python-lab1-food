# Лабораторная работа № 1. Обработка и очистка данных

Датасет: `branded_food.csv` из [USDA FoodData Central](https://fdc.nal.usda.gov/download-datasets) (около 2 млн строк, 21 столбец).
Описание столбцов есть в [DATASET.md](DATASET.md).

Что сделано: данные из CSV переведены в Parquet, приведены к нужным типам, очищены и загружены в SQLite и PostgreSQL.

Python 3.12.14.

## Установка

```
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install poetry
poetry config virtualenvs.in-project true
poetry install
copy .env.example .env
```

В `.env` нужно указать данные для подключения к PostgreSQL.
Файл `branded_food.csv` кладётся в `data/raw/` (папка `data/` в репозиторий не попадает).

## Запуск

```
python -m src.pipeline --mode debug     # на срезе в 200 тыс. строк
python -m src.pipeline                  # на всём наборе
python -m src.validation data/processed/clean.parquet
python -m src.benchmarks all            # замеры форматов, типов, pandas и polars
python -m src.db sqlite
python -m src.db postgres
```

Блокноты лежат в `notebooks/`: разведка данных (`01_overview`), очистка по шагам (`02_cleaning`) и версия для Google Colab (`03_colab`).

## Папки

- `data/` – исходные, промежуточные и очищенные данные
- `src/` – код
- `sql/` – схемы баз данных
- `results/` – результаты замеров и журнал очистки
- `docs/` – ER-диаграмма
