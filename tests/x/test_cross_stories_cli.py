"""Регресс ТЗ-7 (P0): CLI сквозных сюжетов не падает на печати ссылок.

Живой прогон 17.09.2026 (журнал ``tuber_x_cross_stories.log``): сюжеты
посчитались и вывелись, но в конце падало
``AttributeError: module 'tuber.analysis.report' has no attribute 'content_url'``
(код возврата 1 → ложная тревога владельцу). Причина — старый
четырёхаргументный вызов хелпера, который после ТЗ-6 живёт в
:mod:`tuber.core.urls` и принимает три аргумента.

Тест бьёт ИМЕННО по CLI-пути ``cmd_cross_stories`` (функция печати в
``tuber/platforms/x/cli.py``) на временной базе. У сюжета четыре участника:
с заполненным ``content.url``, без ``url`` (ссылка синтезируется), и участник
без ссылки вовсе (честное «нет ссылки»). Сеть не используется.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tuber.platforms.x import cli, config, store as xdb
from tuber.platforms.x.cli import build_parser

NOW = datetime.now(timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def core_db(tmp_path, monkeypatch):
    """Временное ядро: сквозной сюжет с участниками разных видов ссылок."""
    path = str(tmp_path / "core.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    con = xdb.init_db(path)
    con.execute("INSERT INTO source(id, platform, external_id, handle)"
                " VALUES (1,'x','sx','srcx'),(2,'telegram','stg','realtg'),"
                "       (3,'youtube','syt','srcyt')")
    link_text = ("SkyVenture @skyventure launches the Qwen vision model with open "
                 "weights today, benchmarks and deployment notes inside")
    # 1) url заполнен (D-45) — печататься должен он, а не синтез;
    # 2) url пуст, но есть author_handle — ссылка синтезируется хелпером;
    # 3) url пуст у YouTube — синтез всегда возможен по external_id;
    # 4) url пуст и нет ни handle, ни канала в external_id — «нет ссылки».
    con.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,"
        "text,url,author_handle) VALUES"
        " (1,'telegram',2,'realtg/10',?,?, 'https://t.me/real_from_db/10','realtg'),"
        " (2,'x',1,'999',?,?, NULL,'alice'),"
        " (3,'youtube',3,'vid9',?,?, NULL,NULL),"
        " (4,'telegram',2,'noslash',?,?, NULL,NULL)",
        (_iso(3), link_text, _iso(2), link_text, _iso(4), link_text, _iso(5), link_text))
    con.execute(
        "INSERT INTO story(id, platform, created_at, title, content_count, xconf,"
        " is_single, suspect) VALUES (1,'cross',?,'сквозной регресс',4,4,0,0)",
        (_iso(1),))
    con.execute(
        "INSERT INTO story_member(story_id,content_id,role,is_canonical,added_at,handle)"
        " VALUES (1,1,'primary',1,?,NULL),(1,2,'echo',0,?,NULL),"
        "        (1,3,'echo',0,?,NULL),(1,4,'echo',0,?,NULL)",
        (_iso(1), _iso(1), _iso(1), _iso(1)))
    con.commit()
    yield path
    con.close()


def test_cross_stories_cli_prints_links_without_attr_error(core_db, capsys):
    """CLI-вывод сквозных сюжетов: exit 0, ссылка из базы, синтез, «нет ссылки»."""
    args = build_parser().parse_args(
        ["cross-stories", "--window", "240", "--text-min", "0.15", "--dry-run"])
    rc = cli.cmd_cross_stories(args)
    out = capsys.readouterr().out

    assert rc == 0
    # 2) url пуст → синтез по author_handle.
    assert "https://x.com/alice/status/999" in out
    # 3) YouTube синтезируется по external_id.
    assert "https://www.youtube.com/watch?v=vid9" in out
    # 1) непустой content.url печатается как есть (приоритет базы, D-45).
    assert "https://t.me/real_from_db/10" in out
    # 4) не хватает компонентов — честное «нет ссылки», а не выдуманная.
    assert "нет ссылки" in out
    # выдуманной ссылки на участника без компонентов быть не должно.
    assert "https://t.me/noslash" not in out
