"""Проверка согласованности результатов: точное сравнение схемы и всех ячеек.

    python -m src.validation data/processed/clean.parquet data/processed/clean_notebook.parquet
"""
import sys

import pandas as pd
from pandas.testing import assert_frame_equal

from . import io_utils as io


def compare(a_path, b_path) -> bool:
    a, b = io.read_parquet(a_path), io.read_parquet(b_path)
    try:
        assert_frame_equal(a, b, check_exact=True, check_categorical=False)
    except AssertionError as e:
        print("РАСХОЖДЕНИЕ:", str(e)[:500])
        return False
    print(f"Совпадают построчно и по типам: {a.shape[0]} строк, {a.shape[1]} столбцов")
    return True


def check_idempotent(clean_path) -> bool:
    """Повторная очистка уже очищенных данных не должна ничего менять."""
    from . import cleaning as cl
    df = io.read_parquet(clean_path)
    again = cl.clean_all(df.copy(), cl.Stats(), None)
    try:
        # dtype строк и порядок категорий после записи в Parquet могут отличаться формально;
        # сравниваются значения всех ячеек
        assert_frame_equal(df, again, check_exact=True, check_dtype=False, check_categorical=False)
    except AssertionError as e:
        print("НЕ идемпотентно:", str(e)[:500])
        return False
    print("Идемпотентность подтверждена: повторная очистка не изменила таблицу")
    return True


if __name__ == "__main__":
    if len(sys.argv) == 3:
        sys.exit(0 if compare(*sys.argv[1:]) else 1)
    if len(sys.argv) == 2:
        sys.exit(0 if check_idempotent(sys.argv[1]) else 1)
    sys.exit("Использование: python -m src.validation A.parquet [B.parquet]")
