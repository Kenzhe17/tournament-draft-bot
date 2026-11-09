"""Hall of Fame 2.0 — Зал славы r!z3 (/booyah).

Читает статистику из PostgreSQL и существующих сторов, ничего не
изменяя. См. records.py (вычисление), view.py (V2-интерфейс).
"""

from services.hall_of_fame.records import CATEGORIES, compute_all
from services.hall_of_fame.cache import get_cached

__all__ = ["CATEGORIES", "compute_all", "get_cached"]
