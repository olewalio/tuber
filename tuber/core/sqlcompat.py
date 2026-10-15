"""Совместимость SQL: legacy-имена, конфликтующие с таблицами ядра (ТЗ-3c).

Зачем
-----
Адаптеры платформ отдают legacy-коду ВРЕМЕННЫЕ (``TEMP``) представления с
прежними именами таблиц (``run_log``, ``blocklist``, ``llm_usage``, …) и
``INSTEAD OF``-триггеры, раскладывающие legacy-запись по таблицам ядра.

Триггер ``TEMP``-схемы разрешает НЕквалифицированные имена сначала в ``TEMP``,
и только потом в ``main``. Поэтому если имя представления совпадает с именем
таблицы ядра (ниже — «конфликтующие»), запись из триггера попадает не в ядро, а
обратно в представление:

* либо ``sqlite3.OperationalError: qualified table names are not allowed on
  INSERT, UPDATE, and DELETE statements within triggers`` — если цель
  квалифицировать ``main.`` (SQLite ≤ 3.45; именно этот дефект — ТЗ-3c),
* либо «тихая» потеря записи — при ``recursive_triggers=OFF`` повторный
  ``INSERT`` в то же представление ничего не делает.

Обойти это одним лишь ``main.`` нельзя: на 3.45.1 квалифицированный DML в теле
триггера запрещён безусловно (проверено и для ``main.``, и для ``"main"``,
``[main]``, и для привязанной схемы, и для триггеров ``main``-схемы).

Решение
-------
Для конфликтующих имён представление совместимости создаётся под ДРУГИМ именем
(суффикс-префикс платформы, например ``x_run_log``). Тогда ``TEMP``-схема
больше не затеняет таблицу ядра, и DML в триггере идёт БЕЗ префикса схемы (как
и требует SQLite). А чтобы legacy-код по-прежнему видел привычное имя,
:class:`CompatConnection` на лету переписывает в исполняемом SQL ИМЯ ТАБЛИЦЫ в
позициях ``FROM`` / ``INTO`` / ``JOIN`` / ``UPDATE`` / ``TABLE`` на имя
представления. Переписывается только имя таблицы; ``main.<имя>`` (прямая работа
с ядром) и строковые литералы/комментарии не трогаются.

Ограничение
-----------
Трансляция включена только на соединениях адаптера и только после установки
слоя совместимости (:func:`enable` / :func:`disable`). Пока слой не поставлен
(миграция схемы ядра), SQL исполняется как есть: там имена конфликтующих таблиц
означают именно таблицы ядра.
"""

from __future__ import annotations

import re
import sqlite3
import sys

# Позиции, в которых за именем следует ИМЯ ТАБЛИЦЫ. Остальные употребления
# (колонки, алиасы, строки) не трогаем — так трансляция не может «уехать».
_TABLE_INTRO = r"(?:FROM|INTO|JOIN|UPDATE|TABLE)"

# Разбиение SQL на «код» и «не код» (строковые литералы и комментарии).
_NON_CODE = re.compile(
    r"('(?:[^']|'(?='))*'|--[^\n]*|/\*.*?\*/)",
    re.DOTALL,
)


def _sub_table_names(code: str, mapping: dict[str, str]) -> str:
    """Переписать имена конфликтующих таблиц в фрагменте «кода»."""
    if not mapping:
        return code
    names = "|".join(re.escape(name) for name in sorted(mapping, key=len, reverse=True))
    # 1) имена таблиц после FROM/INTO/JOIN/UPDATE/TABLE;
    after_intro = re.compile(
        r"\b(%s)\s+(\"?)(%s)\2(?![\w])" % (_TABLE_INTRO, names),
        re.IGNORECASE,
    )
    # 2) имя таблицы внутри PRAGMA-функций: table_info(run_log), index_list(…)…
    #    ``main.table_info(...)`` (прямая работа с ядром) не трогаем.
    in_pragma = re.compile(
        r"(?<![\w.])(table_info|table_xinfo|index_list|index_info|foreign_key_list)"
        r"(\s*\(\s*)(\"?)(%s)\3(?![\w])" % names,
        re.IGNORECASE,
    )

    def repl_intro(match: re.Match[str]) -> str:
        keyword, quote, name = match.group(1), match.group(2), match.group(3)
        return "%s %s%s%s" % (keyword, quote, mapping[name.lower()], quote)

    def repl_pragma(match: re.Match[str]) -> str:
        func, open_, quote, name = match.group(1), match.group(2), match.group(3), match.group(4)
        return "%s%s%s%s%s" % (func, open_, quote, mapping[name.lower()], quote)

    code = after_intro.sub(repl_intro, code)
    return in_pragma.sub(repl_pragma, code)


def translate_legacy_sql(sql: str, mapping: dict[str, str]) -> str:
    """Заменить legacy-имена конфликтующих таблиц на имена представлений.

    Строковые литералы и комментарии сохраняются дословно. ``main.<имя>`` не
    трогается: имя таблицы должно идти непосредственно за ``FROM``/``INTO``/
    ``JOIN``/``UPDATE``/``TABLE``.
    """
    if not mapping or not sql:
        return sql
    out: list[str] = []
    pos = 0
    for match in _NON_CODE.finditer(sql):
        out.append(_sub_table_names(sql[pos:match.start()], mapping))
        out.append(match.group(0))
        pos = match.end()
    out.append(_sub_table_names(sql[pos:], mapping))
    return "".join(out)


class CompatConnection(sqlite3.Connection):
    """Соединение адаптера с трансляцией legacy-имён конфликтующих таблиц.

    Наследники задают :attr:`collision_map` (legacy-имя → имя TEMP-представления)
    и включают трансляцию через :meth:`enable_compat_sql` после установки слоя
    совместимости. До этого (и после :meth:`disable_compat_sql`) SQL идёт как
    есть.
    """

    collision_map: dict[str, str] = {}

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._compat_sql_enabled = False

    # ------------------------------------------------------------- управление
    def enable_compat_sql(self, mapping: dict[str, str] | None = None) -> None:
        if mapping is not None:
            self.collision_map = dict(mapping)
        self._compat_sql_enabled = True

    def disable_compat_sql(self) -> None:
        self._compat_sql_enabled = False

    @property
    def compat_sql_enabled(self) -> bool:
        return self._compat_sql_enabled

    # -------------------------------------------------------------- трансляция
    def _translate(self, sql: str) -> str:
        if self._compat_sql_enabled and self.collision_map:
            return translate_legacy_sql(sql, self.collision_map)
        return sql

    def execute(self, sql, parameters=()):  # type: ignore[override]
        return sqlite3.Connection.execute(self, self._translate(sql), parameters)

    def executemany(self, sql, seq_of_parameters):  # type: ignore[override]
        return sqlite3.Connection.executemany(
            self, self._translate(sql), seq_of_parameters
        )

    def executescript(self, sql_script):  # type: ignore[override]
        return sqlite3.Connection.executescript(self, self._translate(sql_script))


def enable_compat_sql(conn: sqlite3.Connection, mapping: dict[str, str] | None = None) -> None:
    """Включить трансляцию, если соединение её поддерживает (иначе no-op)."""
    enable = getattr(conn, "enable_compat_sql", None)
    if callable(enable):
        enable(mapping)


def disable_compat_sql(conn: sqlite3.Connection) -> None:
    """Выключить трансляцию, если соединение её поддерживает (иначе no-op)."""
    disable = getattr(conn, "disable_compat_sql", None)
    if callable(disable):
        disable()


# ---------------------------------------------------------------------------
# Ранний явный отказ и диагностика (ТЗ-3c, задача 2)
# ---------------------------------------------------------------------------

# Минимум для слоя совместимости: TEMP-представления + INSTEAD OF-триггеры +
# встроенный JSON1 (``json_extract``/``json_set`` в представлениях и триггерах).
MIN_SQLITE_VERSION = (3, 24, 0)


def sqlite_info() -> dict[str, object]:
    """Версия SQLite, путь и версия интерпретатора — для диагностики."""
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_version_info": tuple(sqlite3.sqlite_version_info),
        "python_version": sys.version.split()[0],
        "executable": sys.executable,
    }


def format_sqlite_info() -> str:
    info = sqlite_info()
    return (
        f"интерпретатор: {info['executable']} (Python {info['python_version']}); "
        f"SQLite {info['sqlite_version']}"
    )


def ensure_supported_sqlite() -> None:
    """Понятный отказ на слишком старом SQLite вместо сырой ошибки из недр.

    Требование ТЗ-3c п.2: версия, путь к интерпретатору и подсказка.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        need = ".".join(str(p) for p in MIN_SQLITE_VERSION)
        raise RuntimeError(
            "неподдерживаемая версия SQLite: %s (нужна >= %s).\n"
            "  интерпретатор: %s (Python %s)\n"
            "Подсказка: запустите контур интерпретатором со свежим SQLite "
            "(например, из venv проекта) — обёртки расписания в scripts/ уже "
            "закрепляют такой интерпретатор; диагностика: `python3 -m tuber doctor`."
            % (
                sqlite3.sqlite_version,
                need,
                sys.executable,
                sys.version.split()[0],
            )
        )


def compat_install_error(exc: BaseException) -> Exception:
    """Обернуть сбой установки слоя совместимости понятным сообщением."""
    error = RuntimeError(
        "не удалось установить слой совместимости адаптера (SQLite %s).\n"
        "  интерпретатор: %s (Python %s)\n"
        "  исходная ошибка: %s\n"
        "Подсказка: скорее всего несовместима версия SQLite или сборка без "
        "нужных функций (JSON1). Запустите контур интерпретатором из venv "
        "проекта; см. `python3 -m tuber doctor`."
        % (
            sqlite3.sqlite_version,
            sys.executable,
            sys.version.split()[0],
            exc,
        )
    )
    error.__cause__ = exc
    return error

