"""Тесты загрузки переменных окружения (tuber.config.load_env)."""

from __future__ import annotations

import os

from tuber.platforms.youtube import config


def test_load_env_missing_file_returns_zero(tmp_path):
    """Нет файла — 0 и никакого падения."""
    assert config.load_env(str(tmp_path / "nope.env")) == 0


def test_load_env_strips_quotes(tmp_path, monkeypatch):
    """Обрамляющие одинарные и двойные кавычки снимаются."""
    monkeypatch.delenv("TUBER_TEST_Q1", raising=False)
    monkeypatch.delenv("TUBER_TEST_Q2", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'TUBER_TEST_Q1="hello world"\n'
        "TUBER_TEST_Q2='single value'\n",
        encoding="utf-8",
    )
    assert config.load_env(str(env)) == 2
    assert os.environ["TUBER_TEST_Q1"] == "hello world"
    assert os.environ["TUBER_TEST_Q2"] == "single value"


def test_load_env_skips_comments_and_blank_lines(tmp_path, monkeypatch):
    """Комментарии, пустые строки и строки без '=' пропускаются."""
    monkeypatch.delenv("TUBER_TEST_C1", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# это комментарий\n"
        "\n"
        "   \n"
        "TUBER_TEST_C1=ok\n"
        "# TUBER_TEST_C2=нет\n"
        "NO_EQUALS_HERE\n",
        encoding="utf-8",
    )
    assert config.load_env(str(env)) == 1
    assert os.environ["TUBER_TEST_C1"] == "ok"
    assert "TUBER_TEST_C2" not in os.environ


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    """Уже заданная извне переменная не перетирается."""
    monkeypatch.setenv("TUBER_TEST_KEEP", "external")
    env = tmp_path / ".env"
    env.write_text("TUBER_TEST_KEEP=from-file\n", encoding="utf-8")
    assert config.load_env(str(env)) == 0
    assert os.environ["TUBER_TEST_KEEP"] == "external"


def test_load_env_counts_only_new_vars(tmp_path, monkeypatch):
    """Счётчик считает только реально проставленные переменные."""
    monkeypatch.setenv("TUBER_TEST_OLD", "keep")
    monkeypatch.delenv("TUBER_TEST_NEW", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "TUBER_TEST_OLD=file\n"
        "TUBER_TEST_NEW=fresh\n",
        encoding="utf-8",
    )
    assert config.load_env(str(env)) == 1
    assert os.environ["TUBER_TEST_OLD"] == "keep"
    assert os.environ["TUBER_TEST_NEW"] == "fresh"


# --- порог шортса ----------------------------------------------------------


def test_shorts_threshold_default_and_boundary(monkeypatch):
    """По умолчанию 180 с; ровно на пороге — шортс, на секунду больше — нет."""
    monkeypatch.delenv("SHORTS_MAX_SECONDS", raising=False)
    assert config.shorts_max_seconds() == 180
    assert config.is_probable_shorts(180) == 1
    assert config.is_probable_shorts(181) == 0
    assert config.is_probable_shorts(0) == 0
    assert config.is_probable_shorts(61) == 1
    assert config.is_probable_shorts(None) is None


def test_shorts_threshold_reads_env(monkeypatch):
    """Порог перекрывается переменной окружения в момент вызова."""
    monkeypatch.setenv("SHORTS_MAX_SECONDS", "60")
    assert config.shorts_max_seconds() == 60
    assert config.is_probable_shorts(60) == 1
    assert config.is_probable_shorts(61) == 0


def test_shorts_threshold_bad_env_falls_back(monkeypatch):
    """Мусор в окружении не ломает порог: берём значение по умолчанию."""
    monkeypatch.setenv("SHORTS_MAX_SECONDS", "не число")
    assert config.shorts_max_seconds() == 180


def test_shorts_threshold_from_cfg(monkeypatch):
    """Порог можно взять из переданного конфига."""
    monkeypatch.delenv("SHORTS_MAX_SECONDS", raising=False)

    class Cfg:
        SHORTS_MAX_SECONDS = 90

    assert config.shorts_max_seconds(Cfg) == 90
    assert config.is_probable_shorts(90, Cfg) == 1
    assert config.is_probable_shorts(91, Cfg) == 0


# --- порог интервала для расчёта скорости ----------------------------------


def test_speed_interval_default(monkeypatch):
    """По умолчанию час; имя из ТЗ доступно как алиас."""
    monkeypatch.delenv("MIN_INTERVAL_FOR_SPEED_SECONDS", raising=False)
    monkeypatch.delenv("MIN_INTERVAL_FOR_SPED_SECONDS", raising=False)
    assert config.min_interval_for_speed_seconds() == 3600
    assert config.MIN_INTERVAL_FOR_SPEED_SECONDS == 3600
    assert config.MIN_INTERVAL_FOR_SPED_SECONDS == 3600


def test_speed_interval_reads_env(monkeypatch):
    """Порог перекрывается переменной окружения в момент вызова."""
    monkeypatch.setenv("MIN_INTERVAL_FOR_SPEED_SECONDS", "900")
    assert config.min_interval_for_speed_seconds() == 900
    monkeypatch.setenv("MIN_INTERVAL_FOR_SPED_SECONDS", "1200")
    monkeypatch.delenv("MIN_INTERVAL_FOR_SPEED_SECONDS", raising=False)
    assert config.min_interval_for_speed_seconds() == 1200


def test_speed_interval_bad_env_falls_back(monkeypatch):
    """Мусор в окружении не ломает порог: берём значение по умолчанию."""
    monkeypatch.setenv("MIN_INTERVAL_FOR_SPEED_SECONDS", "не число")
    monkeypatch.delenv("MIN_INTERVAL_FOR_SPED_SECONDS", raising=False)
    assert config.min_interval_for_speed_seconds() == 3600


def test_speed_interval_from_cfg(monkeypatch):
    """Порог можно взять из переданного конфига."""
    monkeypatch.delenv("MIN_INTERVAL_FOR_SPEED_SECONDS", raising=False)
    monkeypatch.delenv("MIN_INTERVAL_FOR_SPED_SECONDS", raising=False)

    class Cfg:
        MIN_INTERVAL_FOR_SPEED_SECONDS = 1800

    assert config.min_interval_for_speed_seconds(Cfg) == 1800


# --- лимит разбора handle (расширение поиска) ------------------------------


def test_expand_handle_resolve_limit_is_400():
    """Лимит разбора имён — 400, выбран по замеру очереди (D-07).

    Замер 13.09.2026: очередь к разбору ≈ 363 (240 отложенных @handle плюс
    ~123 свежих упоминания, добавляемых прогоном), приток упоминаний
    152–362/сут, а прогон при лимите 250 вставал по лимиту разбора, а не по
    бюджету (675 units из 2000). 400 закрывает очередь с запасом и укладывается
    в бюджет прогона: разбор одного имени стоит 1 unit.
    """
    assert config.EXPAND_MAX_HANDLE_RESOLVES_PER_RUN == 400
    assert (config.EXPAND_MAX_HANDLE_RESOLVES_PER_RUN
            <= config.EXPAND_BUDGET_UNITS_PER_RUN)


# --- магические дефолты лимитов вынесены в конфиг (ТЗ-35, п. 3) -------------
#
# Раньше числа стояли прямо в вызове getattr(cfg, "ИМЯ", ЧИСЛО), и значение по
# умолчанию не было видно в конфиге. Теперь источник один — этот модуль.

_MOVED_DEFAULTS = {
    "SHORTS_FRESH_SLOT_SPEDUP": 1.5,
    "DAILY_TWICE_INTERVAL_SECONDS": 12 * 3600,
    "MAX_TRACK_DAYS": 30,
    "MAX_VIDEO_AGE_DAYS": 30,
    "MAX_UPLOAD_PAGES_PER_CHANNEL": 2,
    "COLLECT_QUERIES_PER_RUN": 40,
    "COLLECT_TIME_BUDGET_SECONDS": 900,
    "MIN_AI_VIDEOS_PER_CHANNEL": 2,
    "EXPAND_MAX_PROBES_PER_RUN": 40,
    "EXPAND_MAX_NEW_CHANNELS_PER_RUN": 30,
    "EXPAND_PROBE_VIDEOS": 15,
    "EXPAND_MAX_SEARCH_FALLBACKS": 3,
    "EXPAND_MAX_PLAYLISTS_PER_CHANNEL": 3,
    "EXPAND_MAX_CHANNEL_SEARCHES_PER_RUN": 2,
    "EXPAND_PHRASE_FAIL_LIMIT": 3,
    "EXPAND_PHRASE_FAIL_COOLDOWN_SEC": 21600,
    # В вызове стоял мёртвый литерал 40, но cfg по умолчанию — сам модуль config,
    # где значение 20; поведение сохраняем (20), мёртвый литерал убран.
    "EXPAND_MAX_PLAYLIST_CHANNELS_PER_RUN": 20,
}


def test_moved_defaults_match_config_values():
    """Вынесенные константы существуют и равны прежним рабочим значениям.

    Инвариант поведения: числа те же, менялось только их место. Тест проходит и
    на fdc4186 — это защита от случайного изменения значений, а не доказательство
    переноса; перенос доказывает test_no_magic_numeric_defaults_in_tuber.
    """
    for name, expected in _MOVED_DEFAULTS.items():
        assert hasattr(config, name), name
        assert getattr(config, name) == expected, name


def test_no_magic_numeric_defaults_in_tuber():
    """Ни один числовой дефолт не спрятан в вызове getattr в tuber/.

    На fdc4186 таких вызовов было 23 (17 различных констант): collect.py — 6,
    expand.py — 14, schedule.py — 3; после переноса чисел в config.py их не
    остаётся. Проверка по исходникам, без сети.
    """
    import re
    from pathlib import Path

    pattern = re.compile(
        r'getattr\([^()]*?,\s*["\']([A-Z_][A-Z0-9_]*)["\']\s*,\s*[0-9]'
    )
    root = Path(config.__file__).resolve().parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line}: {match.group(1)}")
    assert offenders == [], offenders



# --- порог показов и потолок оси виральности (ТЗ порога показов) -----------


def test_viral_min_views_defaults_per_stream(monkeypatch):
    """По умолчанию порог один и тот же для потоков, но задаётся раздельно."""
    for name in (config.VIRAL_MIN_VIEWS_ENV, config.VIRAL_MIN_VIEWS_SHORTS_ENV,
                 config.VIRAL_MIN_VIEWS_LONG_ENV):
        monkeypatch.delenv(name, raising=False)
    assert config.viral_min_views("short") == config.VIRAL_MIN_VIEWS
    assert config.viral_min_views("long") == config.VIRAL_MIN_VIEWS
    assert config.viral_min_views(None) == config.VIRAL_MIN_VIEWS


def test_viral_min_views_env_override_per_stream(monkeypatch):
    monkeypatch.setenv(config.VIRAL_MIN_VIEWS_ENV, "1000")
    monkeypatch.setenv(config.VIRAL_MIN_VIEWS_SHORTS_ENV, "5000")
    monkeypatch.setenv(config.VIRAL_MIN_VIEWS_LONG_ENV, "300")
    assert config.viral_min_views("short") == 5000
    assert config.viral_min_views("long") == 300
    # Неизвестный поток берёт общий порог.
    assert config.viral_min_views("all") == 1000


def test_viral_min_views_ignores_garbage_env(monkeypatch):
    monkeypatch.setenv(config.VIRAL_MIN_VIEWS_ENV, "не число")
    assert config.viral_min_views(None) == config.VIRAL_MIN_VIEWS
    monkeypatch.setenv(config.VIRAL_MIN_VIEWS_ENV, "-5")
    assert config.viral_min_views(None) == config.VIRAL_MIN_VIEWS


def test_viral_axis_cap_env_override_and_disable(monkeypatch):
    monkeypatch.delenv(config.VIRAL_AXIS_CAP_ENV, raising=False)
    assert config.viral_axis_cap() == config.VIRAL_AXIS_CAP
    monkeypatch.setenv(config.VIRAL_AXIS_CAP_ENV, "10.5")
    assert config.viral_axis_cap() == 10.5
    # 0 означает «без потолка» и допустим.
    monkeypatch.setenv(config.VIRAL_AXIS_CAP_ENV, "0")
    assert config.viral_axis_cap() == 0.0
    monkeypatch.setenv(config.VIRAL_AXIS_CAP_ENV, "мусор")
    assert config.viral_axis_cap() == config.VIRAL_AXIS_CAP
