"""Регресс 17.09.2026: живой пул Nitter и покрытие скоринга X.

Два дефекта, которые эти тесты должны не пустить обратно:

1. **Рассинхрон «свой инстанс вне пула».** Значение из ``INSTANCE_OWNERSHIP``
   обязано присутствовать в ``INSTANCES``: иначе транспорт при "dedicated"
   уходит на адрес, которого нет в пуле, и сбор валится отказами (в замере
   17.09.2026 — 683 отказа из 1 064 запросов ленты, 39%, из-за мёртвого
   ``nitter.kareem.one`` в роли аварийного инстанса).

2. **Откат лимита скоринга.** Обёртка ``scripts/x/tuber_x_scores.sh`` обязана
   передавать ``--limit`` не меньше 500: прежний лимит 50 писал в таблицу
   оценок только ~50 свежих строк, и большинство годных постов X не могло
   попасть в выдачу. Замер стоимости: ``--limit 400`` = 3.1 с,
   ``--limit 2000`` = 3.2 с, поэтому 500 недорого.

Тесты не ходят в сеть и не трогают базы: только чтение конфига и файла обёртки.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCORES_WRAPPER = os.path.join(ROOT, "scripts", "x", "tuber_x_scores.sh")


def test_instances_no_duplicates_and_at_least_two():
    """Пул: без дублей и не менее двух адресов (нужен аварийный fallback)."""
    from tuber.platforms.x import config

    assert len(config.INSTANCES) >= 2, (
        "в пуле меньше двух инстансов: аварийный fallback невозможен")
    normalized = [h.rstrip("/") for h in config.INSTANCES]
    assert len(normalized) == len(set(normalized)), (
        f"в INSTANCES дубли: {config.INSTANCES}")
    for host in config.INSTANCES:
        assert host.startswith("http://") or host.startswith("https://"), (
            f"адрес инстанса без схемы: {host!r}")


def test_ownership_instances_are_inside_pool():
    """Каждый закреплённый инстанс обязан входить в пул (иначе — отказы сбора)."""
    from tuber.platforms.x import config

    pool = {h.rstrip("/") for h in config.INSTANCES}
    assert config.INSTANCE_OWNERSHIP, "INSTANCE_OWNERSHIP пуст"
    for owner, host in config.INSTANCE_OWNERSHIP.items():
        assert host.rstrip("/") in pool, (
            f"инстанс {host!r} закреплён за {owner!r}, но отсутствует в INSTANCES"
            f" {config.INSTANCES}: рассинхрон пула и владения даёт отказы сбора")


# Строка ВЫЗОВА CLI (а не упоминание в комментарии): `... -m tuber x scores --limit N`.
_CMD_RE = {
    "x stories": re.compile(r"-m\s+tuber\s+x\s+stories\s+--limit\s+(\d+)"),
    "x scores": re.compile(r"-m\s+tuber\s+x\s+scores\s+--limit\s+(\d+)"),
}


def test_scores_wrapper_uses_wide_limit():
    """Обёртка скоринга: ``--limit`` в обоих вызовах не меньше 500."""
    with open(SCORES_WRAPPER, encoding="utf-8") as fh:
        text = fh.read()

    for cmd, rx in _CMD_RE.items():
        m = rx.search(text)
        assert m, f"в обёртке нет вызова `{cmd}` с явным --limit"
        value = int(m.group(1))
        assert value >= 500, (
            f"`{cmd}` использует --limit {value}: откат к узкому лимиту"
            " снова спрячет собранные посты из выдачи (нужно >= 500)")
