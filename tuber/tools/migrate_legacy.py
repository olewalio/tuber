"""Миграция данных из трёх legacy-баз в единую базу ядра (ТЗ-1 §5).

Запуск::

    python3 -m tuber migrate --target /root/tuber/data/tuber.db \\
        --os /root/tuber-os/data/tuber.db \\
        --x /root/tuber-x/data/tuber_x.db \\
        --tg /root/tuber-telegram/data/tuber_telegram.db

Правила (обязательные требования ТЗ-1):

1. Legacy-базы открываются ТОЛЬКО read-only. Ни одного INSERT/UPDATE/DELETE по
   ним. Боевые базы не копируются и не перезаписываются.
2. Идемпотентность: повторный прогон не создаёт дублей; числа не меняются.
3. Каждая переехавшая строка пишет запись в ``legacy_map``.
4. Порядок: platform → source → content → metric_snapshot →
   classification/classify_cache → score → story/story_member → остальное.
5. Даты: epoch → ISO через :mod:`tuber.core.timeutil`.
6. Поля-флаги маппятся явно (см. :mod:`docs.SCHEMA` / ``SCHEMA.md``).
7. Данные не выдумываются: если поля нет в legacy — остаётся NULL.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict

from tuber.core import db, ids, legacy, storage, timeutil
from tuber.core.schema import duplicate_index_report, migrate_schema, verify_schema

# Псевдо-таблицы, у которых целевой id — составной/текстовый ключ.
_NO_INT_TARGET = 0


# ---------------------------------------------------------------------------
# Служебные хелперы
# ---------------------------------------------------------------------------

def _val(row: sqlite3.Row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _norm(value):
    """Дата → ISO UTC (или None)."""
    return timeutil.parse_any(value)


def _norm_or_none(value):
    if value in (None, ""):
        return None
    return value


def _display_handle(value):
    """Человекочитаемый handle: убрать ведущие ``@``, пустое → NULL (D-01)."""
    text = _norm_or_none(value)
    if text is None:
        return None
    text = text.lstrip("@").strip()
    return text or None


class Stats:
    """Счётчики переноса и пропусков для итоговой сводки."""

    def __init__(self) -> None:
        self.migrated: dict[tuple[str, str, str], int] = defaultdict(int)
        self.skipped: dict[tuple[str, str], int] = defaultdict(int)
        self.legacy_rows: dict[tuple[str, str], int] = defaultdict(int)
        #: Прогонялся ли в конце ANALYZE (см. TECH-DEBT D-21).
        self.analyzed: bool = False
        #: Лишние адаптерные индексы, удалённые этим прогоном (ТЗ-3d).
        self.dropped_indexes: list[str] = []
        #: Выполнялся ли в конце VACUUM (ТЗ-3d).
        self.vacuumed: bool = False
        #: Прогон ``--schema-only`` (без переноса строк): сводка должна
        #: отчитываться о РАБОТЕ по схеме, а не только о VACUUM/ANALYZE.
        self.schema_only: bool = False
        #: Созданные прогоном объекты схемы: таблицы/индексы/триггеры (ТЗ-3d).
        self.created_tables: list[str] = []
        self.created_indexes: list[str] = []
        self.created_triggers: list[str] = []
        #: Добавленные аддитивные колонки (``table.column``).
        self.added_columns: list[str] = []
        #: Счётчики бэкфилла денормализации ``{объект: строк}`` (ТЗ-2c).
        self.denorm_counts: dict[str, int] = {}
        #: Проблемы схемы ядра до и после прогона (ТЗ-3d, ``verify_schema``).
        self.schema_problems_before: list[str] = []
        self.schema_problems_after: list[str] = []
        #: Остаточные дубли/префикс-дубли индексов после прогона (ТЗ-3d).
        self.duplicate_problems: list[str] = []

    def add(self, legacy_table: str, target_table: str, legacy_db: str, n: int = 1) -> None:
        self.migrated[(legacy_db, legacy_table, target_table)] += n

    def skip(self, legacy_db: str, legacy_table: str, n: int = 1) -> None:
        self.skipped[(legacy_db, legacy_table)] += n

    def seen(self, legacy_db: str, legacy_table: str, n: int = 1) -> None:
        self.legacy_rows[(legacy_db, legacy_table)] += n


# ---------------------------------------------------------------------------
# tuber-os
# ---------------------------------------------------------------------------

def migrate_os(target: sqlite3.Connection, path: str, stats: Stats) -> None:
    src = legacy.open_legacy(path)
    try:
        with db.write_tx(target):
            _migrate_os_sources(target, src, stats)
            _migrate_os_content(target, src, stats)
            _migrate_os_snapshots(target, src, stats)
            _migrate_os_scores(target, src, stats)
            _migrate_os_classification(target, src, stats)
            _migrate_os_extensions(target, src, stats)
            _migrate_os_candidates(target, src, stats)
            _migrate_os_quota(target, src, stats)
            _migrate_os_llm(target, src, stats)
            _migrate_os_topics(target, src, stats)
    finally:
        src.close()


def _migrate_os_sources(target, src, stats):
    global _OS_SOURCE_MAP
    _OS_SOURCE_MAP = {}
    for row in legacy.rows(src, "channels", order_by="channel_id"):
        stats.seen("os", "channels")
        channel_id = _val(row, "channel_id")
        handle = _norm_or_none(_val(row, "handle")) or str(channel_id)
        meta = {
            "video_count": _val(row, "video_count"),
            "view_count": _val(row, "view_count"),
            "topic_categories": _norm_or_none(_val(row, "topic_categories")),
            "uploads_playlist_id": _norm_or_none(_val(row, "uploads_playlist_id")),
            "is_russian": _val(row, "is_russian"),
        }
        sid = storage.upsert_source(
            target, "youtube", handle,
            external_id=channel_id,
            title=_norm_or_none(_val(row, "title")),
            country=_norm_or_none(_val(row, "country")),
            lang=_norm_or_none(_val(row, "default_language")),
            subs=_val(row, "subscriber_count"),
            first_seen_at=_norm(_val(row, "first_seen")),
            last_synced_at=_norm(_val(row, "last_synced_at")),
            source_kind="youtube",
            meta_json=storage.jdump(meta),
        )
        _OS_SOURCE_MAP[channel_id] = sid
        storage.set_legacy_map(target, "os", "channels", channel_id, "source", sid)
        stats.add("channels", "source", "os")


def _migrate_os_content(target, src, stats):
    global _OS_CONTENT_MAP
    _OS_CONTENT_MAP = {}
    for row in legacy.rows(src, "videos", order_by="video_id"):
        stats.seen("os", "videos")
        video_id = _val(row, "video_id")
        channel_id = _val(row, "channel_id")
        sid = _OS_SOURCE_MAP.get(channel_id)
        if sid is None:
            stats.skip("os", "videos")
            continue
        is_shorts = _val(row, "is_shorts") or 0
        meta = {
            "tags": _norm_or_none(_val(row, "tags")),
            "thumbnail_url": _norm_or_none(_val(row, "thumbnail_url")),
            "thumbnail_width": _val(row, "thumbnail_width"),
            "thumbnail_height": _val(row, "thumbnail_height"),
            "thumbnail_checked_at": _norm(_val(row, "thumbnail_checked_at")),
            "live_broadcast": _norm_or_none(_val(row, "live_broadcast")),
            "caption_available": _val(row, "caption_available"),
            "primary_topic": _norm_or_none(_val(row, "primary_topic")),
            "topic_confidence": _val(row, "topic_confidence"),
        }
        category_id = _val(row, "category_id")
        # TODO(debt-D-45): закрыт в ТЗ-6 — content.url синтезирует общий
        # ядровой хелпер tuber.core.urls внутри storage.upsert_content.
        cid = storage.upsert_content(
            target, "youtube", video_id,
            source_id=sid,
            kind="short" if is_shorts else "video",
            title=_norm_or_none(_val(row, "title")),
            text=_val(row, "description"),
            lang=_norm_or_none(_val(row, "default_language")),
            published_at=_norm(_val(row, "published_at")),
            duration_seconds=_val(row, "duration_seconds"),
            category=str(category_id) if category_id is not None else None,
            is_short=int(bool(is_shorts)),
            first_seen_at=_norm(_val(row, "first_seen")),
            last_seen_at=_norm(_val(row, "last_seen")),
            meta_json=storage.jdump(meta),
        )
        _OS_CONTENT_MAP[video_id] = cid
        storage.set_legacy_map(target, "os", "videos", video_id, "content", cid)
        stats.add("videos", "content", "os")


def _migrate_os_snapshots(target, src, stats):
    for row in legacy.rows(src, "snapshots", order_by="id"):
        stats.seen("os", "snapshots")
        video_id = _val(row, "video_id")
        cid = _OS_CONTENT_MAP.get(video_id)
        if cid is None:
            stats.skip("os", "snapshots")
            continue
        captured_at = _norm(_val(row, "captured_at"))
        if captured_at is None:
            stats.skip("os", "snapshots")
            continue
        raw = {"prev_captured_at": _norm(_val(row, "prev_captured_at"))}
        sid = storage.add_snapshot(
            target, cid, captured_at,
            bucket=_norm_or_none(_val(row, "bucket")),
            source=_norm_or_none(_val(row, "source")),
            age_hours=_val(row, "age_hours"),
            interval_seconds=_val(row, "interval_seconds"),
            interval_quality=_norm_or_none(_val(row, "interval_quality")),
            views=_val(row, "views"),
            likes=_val(row, "likes"),
            comments=_val(row, "comments"),
            delta_views=_val(row, "delta_views"),
            delta_likes=_val(row, "delta_likes"),
            delta_comments=_val(row, "delta_comments"),
            views_per_day=_val(row, "views_per_day"),
            views_per_hour=_val(row, "views_per_hour"),
            is_anomaly=_val(row, "is_anomaly") or 0,
            raw_json=storage.jdump(raw),
        )
        storage.set_legacy_map(
            target, "os", "snapshots", _val(row, "id"), "metric_snapshot", sid
        )
        stats.add("snapshots", "metric_snapshot", "os")


def _migrate_os_scores(target, src, stats):
    for row in legacy.rows(src, "video_scores", order_by="video_id, computed_at"):
        stats.seen("os", "video_scores")
        video_id = _val(row, "video_id")
        cid = _OS_CONTENT_MAP.get(video_id)
        if cid is None:
            stats.skip("os", "video_scores")
            continue
        computed_at = _norm(_val(row, "computed_at"))
        if computed_at is None:
            stats.skip("os", "video_scores")
            continue
        significance = _val(row, "viral_index")
        if significance is None:
            significance = _val(row, "outlier_score")
        axes = {
            "outlier_score": _val(row, "outlier_score"),
            "vpd": _val(row, "vpd"),
            "vpd_ratio": _val(row, "vpd_ratio"),
            "likes_per_1000": _val(row, "likes_per_1000"),
            "comments_per_1000": _val(row, "comments_per_1000"),
            "comment_velocity": _val(row, "comment_velocity"),
            "packaging_score": _val(row, "packaging_score"),
            "viral_index": _val(row, "viral_index"),
        }
        storage.upsert_score(
            target, cid, computed_at,
            significance=significance,
            parts_json=_norm_or_none(_val(row, "score_parts")),
            axes_json=storage.jdump(axes),
        )
        storage.set_legacy_map(
            target, "os", "video_scores",
            f"{video_id}|{_val(row, 'computed_at')}", "score", cid,
        )
        stats.add("video_scores", "score", "os")


def _migrate_os_classification(target, src, stats):
    # TODO(debt-D-03): закрыт в ТЗ-1b — title_ru/summary_ru/reason — см. TECH-DEBT.md.
    for row in legacy.rows(src, "video_classification", order_by="video_id"):
        stats.seen("os", "video_classification")
        video_id = _val(row, "video_id")
        cid = _OS_CONTENT_MAP.get(video_id)
        if cid is None:
            stats.skip("os", "video_classification")
            continue
        # D-03 закрыт: title_ru/summary_ru/reason переносятся один-в-один.
        storage.set_classification(
            target, cid,
            is_ai=_val(row, "is_ai"),
            topic=_norm_or_none(_val(row, "topic")),
            confidence=_val(row, "confidence"),
            lang=_norm_or_none(_val(row, "lang")),
            model=_norm_or_none(_val(row, "model")),
            classified_at=_norm(_val(row, "classified_at")),
            title_ru=_val(row, "title_ru"),
            summary_ru=_val(row, "summary_ru"),
            reason=_val(row, "reason"),
        )
        storage.set_legacy_map(
            target, "os", "video_classification", video_id, "classification", cid
        )
        stats.add("video_classification", "classification", "os")


def _migrate_os_extensions(target, src, stats):
    # seo_fields → seo_field
    if legacy.has_table(src, "seo_fields"):
        cols = [c for c in legacy.columns(src, "seo_fields") if c != "video_id"]
        for row in legacy.rows(src, "seo_fields", order_by="video_id"):
            stats.seen("os", "seo_fields")
            cid = _OS_CONTENT_MAP.get(_val(row, "video_id"))
            if cid is None:
                stats.skip("os", "seo_fields")
                continue
            storage.upsert_seo_field(target, cid, **{c: _val(row, c) for c in cols})
            storage.set_legacy_map(target, "os", "seo_fields", _val(row, "video_id"), "seo_field", cid)
            stats.add("seo_fields", "seo_field", "os")

    # thumbnail_vision → thumbnail_vision
    if legacy.has_table(src, "thumbnail_vision"):
        for row in legacy.rows(src, "thumbnail_vision", order_by="id"):
            stats.seen("os", "thumbnail_vision")
            old_id = _val(row, "id")
            if storage.get_legacy_map(target, "os", "thumbnail_vision", old_id) is not None:
                continue
            cid = _OS_CONTENT_MAP.get(_val(row, "video_id"))
            new_id = storage.add_thumbnail_vision(
                target, cid,
                model=_norm_or_none(_val(row, "model")),
                prompt_version=_norm_or_none(_val(row, "prompt_version")),
                description_raw=_val(row, "description_raw"),
                extracted_text=_val(row, "extracted_text"),
                cost_usd=_val(row, "cost_usd"),
                latency_ms=_val(row, "latency_ms"),
                created_at=_norm(_val(row, "created_at")),
            )
            storage.set_legacy_map(target, "os", "thumbnail_vision", old_id, "thumbnail_vision", new_id)
            stats.add("thumbnail_vision", "thumbnail_vision", "os")

    # video_comments → content_comment
    if legacy.has_table(src, "video_comments"):
        for row in legacy.rows(src, "video_comments", order_by="comment_id"):
            stats.seen("os", "video_comments")
            cid = _OS_CONTENT_MAP.get(_val(row, "video_id"))
            comment_id = _val(row, "comment_id")
            storage.upsert_content_comment(
                target, comment_id,
                content_id=cid,
                author=_val(row, "author"),
                text=_val(row, "text"),
                likes=_val(row, "likes"),
                published_at=_norm(_val(row, "published_at")),
                captured_at=_norm(_val(row, "captured_at")),
                platform="youtube",
            )
            storage.set_legacy_map(
                target, "os", "video_comments", comment_id, "content_comment",
                cid if cid is not None else _NO_INT_TARGET,
            )
            stats.add("video_comments", "content_comment", "os")

    # comment_checks → comment_check
    if legacy.has_table(src, "comment_checks"):
        for row in legacy.rows(src, "comment_checks", order_by="video_id"):
            stats.seen("os", "comment_checks")
            cid = _OS_CONTENT_MAP.get(_val(row, "video_id"))
            if cid is None:
                stats.skip("os", "comment_checks")
                continue
            storage.upsert_comment_check(
                target, cid,
                checked_at=_norm(_val(row, "checked_at")),
                status=_norm_or_none(_val(row, "status")),
                error=_val(row, "error"),
            )
            storage.set_legacy_map(target, "os", "comment_checks", _val(row, "video_id"), "comment_check", cid)
            stats.add("comment_checks", "comment_check", "os")


def _migrate_os_candidates(target, src, stats):
    # TODO(debt-D-01): закрыт в ТЗ-1b — см. TECH-DEBT.md.
    # D-01 закрыт: ключ пула кандидатов — ``handle`` = ``channel_id`` для
    # YouTube-каналов; человекочитаемый handle живёт в ``display_handle``
    # (нормализован: без ведущего ``@``, пустой → NULL). ``found_in_handle``
    # оставлен под «где найден кандидат» и для YouTube-каналов не дублируется.
    # D-06 закрыт: остатки полей — в ``candidate.meta_json``.
    if legacy.has_table(src, "channel_candidates"):
        for row in legacy.rows(src, "channel_candidates", order_by="channel_id"):
            stats.seen("os", "channel_candidates")
            channel_id = _val(row, "channel_id")
            extra = {
                "title": _norm_or_none(_val(row, "title")),
                "description": _norm_or_none(_val(row, "description")),
                "evidence": _norm_or_none(_val(row, "evidence")),
                "subscriber_count": _val(row, "subscriber_count"),
                "resolve_attempts": _val(row, "resolve_attempts"),
                "probed_at": _norm(_val(row, "probed_at")),
            }
            cid = storage.add_candidate(
                target, "youtube", str(channel_id),
                kind="channel",
                external_id=str(channel_id),
                display_handle=_display_handle(_val(row, "handle")),
                found_via=_norm_or_none(_val(row, "source")),
                score_priority=_val(row, "score"),
                seen_count=_val(row, "mentions") or 1,
                first_seen_at=_norm(_val(row, "discovered_at")),
                last_seen_at=_norm(_val(row, "probed_at")),
                reject_reason=_norm_or_none(_val(row, "reject_reason")),
                status=_norm_or_none(_val(row, "status")) or "new",
                meta_json=storage.jdump(extra),
            )
            storage.set_legacy_map(target, "os", "channel_candidates", channel_id, "candidate", cid)
            stats.add("channel_candidates", "candidate", "os")

    # query_candidates → candidate(platform='youtube', kind='query')
    if legacy.has_table(src, "query_candidates"):
        for row in legacy.rows(src, "query_candidates", order_by="query"):
            stats.seen("os", "query_candidates")
            query = _val(row, "query")
            extra = {
                "evidence": _norm_or_none(_val(row, "evidence")),
                "kind": _norm_or_none(_val(row, "kind")),
                "runs": _val(row, "runs"),
                "accepted": _val(row, "accepted"),
                "fail_count": _val(row, "fail_count"),
                "last_fail_at": _norm(_val(row, "last_fail_at")),
            }
            cid = storage.add_candidate(
                target, "youtube", str(query),
                kind="query",
                external_id=str(query),
                found_via=_norm_or_none(_val(row, "source")),
                score_priority=_val(row, "score"),
                seen_count=_val(row, "hits") or 1,
                first_seen_at=_norm(_val(row, "discovered_at")),
                last_seen_at=_norm(_val(row, "last_run_at")),
                reject_reason=_norm_or_none(_val(row, "reject_reason")),
                status=_norm_or_none(_val(row, "status")) or "new",
                meta_json=storage.jdump(extra),
            )
            storage.set_legacy_map(target, "os", "query_candidates", query, "candidate", cid)
            stats.add("query_candidates", "candidate", "os")


def _migrate_os_quota(target, src, stats):
    # TODO(debt-D-02): закрыт в ТЗ-1b — legacy_map.target_id TEXT — см. TECH-DEBT.md.
    agg: dict[tuple, dict] = {}
    for row in legacy.rows(src, "quota_log", order_by="id"):
        stats.seen("os", "quota_log")
        day = timeutil.date_of(_norm(_val(row, "date")))
        if day is None:
            stats.skip("os", "quota_log")
            continue
        key = (_norm_or_none(_val(row, "key_id")) or "", day, _norm_or_none(_val(row, "endpoint")) or "")
        slot = agg.setdefault(key, {"calls": 0, "units": 0, "project": None, "ts": None})
        slot["calls"] += _val(row, "calls") or 0
        slot["units"] += _val(row, "units") or 0
        slot["project"] = slot["project"] or _norm_or_none(_val(row, "project"))
        slot["ts"] = slot["ts"] or _norm(_val(row, "ts"))
        # D-02 закрыт: legacy_map.target_id — TEXT, пишем реальный составной
        # ключ строкой ``platform|key_id|day|endpoint``.
        composite = f"youtube|{key[0]}|{key[1]}|{key[2]}"
        storage.set_legacy_map(target, "os", "quota_log", _val(row, "id"), "quota_usage", composite)
    for (key_id, day, endpoint), slot in agg.items():
        storage.upsert_quota_usage(
            target, "youtube", day, key_id, endpoint,
            project=slot["project"], calls=slot["calls"], units=slot["units"], ts=slot["ts"],
        )
        stats.add("quota_log", "quota_usage", "os")


def _migrate_os_llm(target, src, stats):
    for row in legacy.rows(src, "llm_usage", order_by="id"):
        stats.seen("os", "llm_usage")
        old_id = _val(row, "id")
        if storage.get_legacy_map(target, "os", "llm_usage", old_id) is not None:
            continue
        new_id = storage.add_llm_usage(
            target, "youtube",
            stage=_norm_or_none(_val(row, "stage")),
            model=_norm_or_none(_val(row, "model")),
            tokens_in=_val(row, "tokens_in"),
            tokens_out=_val(row, "tokens_out"),
            cost_usd=_val(row, "cost_usd"),
            created_at=_norm(_val(row, "created_at")),
        )
        storage.set_legacy_map(target, "os", "llm_usage", old_id, "llm_usage", new_id)
        stats.add("llm_usage", "llm_usage", "os")


def _migrate_os_topics(target, src, stats):
    if not legacy.has_table(src, "topics"):
        return
    for row in legacy.rows(src, "topics", order_by="name"):
        stats.seen("os", "topics")
        name = _val(row, "name")
        storage.upsert_topic(target, name, platform="youtube")
        storage.set_legacy_map(target, "os", "topics", name, "topic", _NO_INT_TARGET)
        stats.add("topics", "topic", "os")


# ---------------------------------------------------------------------------
# tuber-x
# ---------------------------------------------------------------------------

def migrate_x(target: sqlite3.Connection, path: str, stats: Stats) -> None:
    src = legacy.open_legacy(path)
    try:
        with db.write_tx(target):
            _migrate_x_sources(target, src, stats)
            _migrate_x_content(target, src, stats)
            _migrate_x_snapshots(target, src, stats)
            _migrate_x_stories(target, src, stats)
            _migrate_x_scores(target, src, stats)
            _migrate_x_classified(target, src, stats)
            _migrate_x_classify_daily(target, src, stats)
            _migrate_x_candidates(target, src, stats)
            _migrate_x_transport(target, src, stats)
            _migrate_x_runs(target, src, stats)
            _migrate_x_misc(target, src, stats)
    finally:
        src.close()


def _migrate_x_sources(target, src, stats):
    global _X_SOURCE_MAP, _X_SOURCE_BY_HANDLE
    _X_SOURCE_MAP = {}
    _X_SOURCE_BY_HANDLE = {}
    for row in legacy.rows(src, "accounts", order_by="id"):
        stats.seen("x", "accounts")
        handle = _val(row, "handle")
        meta = {
            "last_success_at": _norm(_val(row, "last_success_at")),
            "last_attempt_at": _norm(_val(row, "last_attempt_at")),
            "ai_density_src": _norm_or_none(_val(row, "ai_density_src")),
            "provisional_since": _norm(_val(row, "provisional_since")),
            "reject_reason": _norm_or_none(_val(row, "reject_reason")),
            "last_reject_at": _norm(_val(row, "last_reject_at")),
            "promo_path": _norm_or_none(_val(row, "promo_path")),
        }
        sid = storage.upsert_source(
            target, "x", handle,
            external_id=_norm_or_none(_val(row, "x_id")),
            tier=_norm_or_none(_val(row, "tier")),
            status=_norm_or_none(_val(row, "status")),
            lang=_norm_or_none(_val(row, "lang")),
            topic_guess=_norm_or_none(_val(row, "topic_guess")),
            is_author=_val(row, "is_author"),
            ai_density=_val(row, "ai_density"),
            cv_interval=_val(row, "cv_interval"),
            posts_per_day=_val(row, "posts_per_day"),
            link_ratio=_val(row, "link_ratio"),
            rt_ratio=_val(row, "rt_ratio"),
            dup_ratio=_val(row, "dup_ratio"),
            first_mover_score=_val(row, "first_mover_score"),
            posts_collected=_val(row, "posts_collected"),
            fail_streak=_val(row, "fail_streak"),
            last_error=_val(row, "last_error"),
            cursor=_val(row, "cursor"),
            added_at=_norm(_val(row, "added_at")),
            added_by=_norm_or_none(_val(row, "added_by")),
            source_kind=_norm_or_none(_val(row, "source_type")),
            notes=_val(row, "notes"),
            verified_at=_norm(_val(row, "verified_at")),
            first_seen_at=_norm(_val(row, "added_at")),
            last_synced_at=_norm(_val(row, "last_success_at")),
            meta_json=storage.jdump(meta),
        )
        _X_SOURCE_MAP[_val(row, "id")] = sid
        _X_SOURCE_BY_HANDLE[handle] = sid
        storage.set_legacy_map(target, "x", "accounts", _val(row, "id"), "source", sid)
        stats.add("accounts", "source", "x")


def _migrate_x_content(target, src, stats):
    global _X_CONTENT_MAP
    _X_CONTENT_MAP = {}
    for row in legacy.rows(src, "posts", order_by="id"):
        stats.seen("x", "posts")
        tweet_id = _val(row, "tweet_id")
        sid = _X_SOURCE_MAP.get(_val(row, "account_id"))
        meta = {
            "published_src": _norm_or_none(_val(row, "published_src")),
            "owner_handle": _norm_or_none(_val(row, "owner_handle")),
            "orig_handle": _norm_or_none(_val(row, "orig_handle")),
            "has_quote": _val(row, "has_quote"),
            "is_long": _val(row, "is_long"),
            "metrics_at": _norm(_val(row, "metrics_at")),
            "metrics_src": _norm_or_none(_val(row, "metrics_src")),
            "pinned": _val(row, "pinned"),
            "retweet_count": _val(row, "retweet_count"),
            "author_verified": _val(row, "author_verified"),
            "spread_src": _norm_or_none(_val(row, "spread_src")),
            "text_src": _norm_or_none(_val(row, "text_src")),
            "likes": _val(row, "likes"),
            "replies": _val(row, "replies"),
        }
        # TODO(debt-D-45): закрыт в ТЗ-6 — content.url синтезирует общий
        # ядровой хелпер tuber.core.urls внутри storage.upsert_content.
        cid = storage.upsert_content(
            target, "x", str(tweet_id),
            source_id=sid,
            kind="post",
            text=_val(row, "text"),
            text_hash=_norm_or_none(_val(row, "text_hash")),
            lang=_norm_or_none(_val(row, "lang")),
            links=_norm_or_none(_val(row, "links")),
            mentions=_norm_or_none(_val(row, "mentions")),
            hashtags=_norm_or_none(_val(row, "hashtags")),
            author_handle=_norm_or_none(_val(row, "author_handle")),
            published_at=_norm(_val(row, "published_at_utc")),
            media_kind=_norm_or_none(_val(row, "media_kind")),
            is_repost=_val(row, "is_retweet") or 0,
            is_quote=_val(row, "is_quote") or 0,
            is_reply=_val(row, "is_reply") or 0,
            first_seen_at=_norm(_val(row, "first_seen_at")),
            deleted_at=_norm(_val(row, "deleted_at")),
            meta_json=storage.jdump(meta),
        )
        _X_CONTENT_MAP[tweet_id] = cid
        # «Последнее известное» состояние по метрикам поста (метрики постов
        # живут в posts, а история — в post_metrics_history).
        metrics_at = _norm(_val(row, "metrics_at"))
        if metrics_at is not None:
            storage.set_content_latest(
                target, cid,
                captured_at=metrics_at,
                likes=_val(row, "likes"),
                replies=_val(row, "replies"),
            )
        storage.set_legacy_map(target, "x", "posts", _val(row, "id"), "content", cid)
        stats.add("posts", "content", "x")


def _migrate_x_snapshots(target, src, stats):
    for row in legacy.rows(src, "post_metrics_history", order_by="id"):
        stats.seen("x", "post_metrics_history")
        cid = _X_CONTENT_MAP.get(_val(row, "tweet_id"))
        if cid is None:
            stats.skip("x", "post_metrics_history")
            continue
        captured_at = _norm(_val(row, "taken_at"))
        if captured_at is None:
            stats.skip("x", "post_metrics_history")
            continue
        snap = storage.add_snapshot(
            target, cid, captured_at,
            age_hours=_val(row, "age_hours"),
            likes=_val(row, "likes"),
            replies=_val(row, "replies"),
            source=_norm_or_none(_val(row, "src")),
        )
        storage.set_legacy_map(
            target, "x", "post_metrics_history", _val(row, "id"), "metric_snapshot", snap
        )
        stats.add("post_metrics_history", "metric_snapshot", "x")


def _migrate_x_stories(target, src, stats):
    global _X_STORY_MAP
    _X_STORY_MAP = {}
    for row in legacy.rows(src, "stories", order_by="id"):
        stats.seen("x", "stories")
        old_id = _val(row, "id")
        mapped = storage.get_legacy_map(target, "x", "stories", old_id)
        first_cid = _X_CONTENT_MAP.get(_val(row, "first_tweet_id"))
        fm_sid = _X_SOURCE_BY_HANDLE.get(_val(row, "first_mover"))
        if mapped is not None:
            _X_STORY_MAP[old_id] = mapped
            continue
        new_id = storage.add_story(
            target,
            platform="x",
            created_at=_norm(_val(row, "created_at")),
            window_hours=_val(row, "window_hours"),
            threshold=_val(row, "threshold"),
            first_content_id=first_cid,
            first_mover_source_id=fm_sid,
            first_pub_at=_norm(_val(row, "published_at")),
            xconf=_val(row, "xconf") or 0,
            content_count=_val(row, "post_count") or 0,
            lead_time_min=_val(row, "lead_time_min"),
            topics=_norm_or_none(_val(row, "topics")),
            entities=_norm_or_none(_val(row, "entities")),
            is_new_entity=_val(row, "is_new_entity") or 0,
            is_single=_val(row, "is_single") or 0,
            suspect=_val(row, "suspect") or 0,
            claimed_at=_norm(_val(row, "claimed_at")),
        )
        _X_STORY_MAP[old_id] = new_id
        storage.set_legacy_map(target, "x", "stories", old_id, "story", new_id)
        stats.add("stories", "story", "x")

    for row in legacy.rows(src, "story_posts", order_by="story_id, tweet_id"):
        stats.seen("x", "story_posts")
        story_id = _X_STORY_MAP.get(_val(row, "story_id"))
        cid = _X_CONTENT_MAP.get(_val(row, "tweet_id"))
        if story_id is None or cid is None:
            stats.skip("x", "story_posts")
            continue
        role = _norm_or_none(_val(row, "role"))
        storage.add_story_member(
            target, story_id, cid,
            role=role,
            is_canonical=1 if role == "primary" else 0,
            added_at=_norm(_val(row, "added_at")),
            # Автор поста на момент кластеризации (legacy story_posts.handle):
            # без него отчёт/«тёмные лошадки» считали бы автора по текущей
            # строке content, а CDN-обогащение могло её переписать (D-26).
            handle=_norm_or_none(_val(row, "handle")),
        )
        storage.set_legacy_map(
            target, "x", "story_posts",
            f"{_val(row, 'story_id')}|{_val(row, 'tweet_id')}", "story_member", cid,
        )
        stats.add("story_posts", "story_member", "x")


def _migrate_x_scores(target, src, stats):
    for row in legacy.rows(src, "scores", order_by="tweet_id"):
        stats.seen("x", "scores")
        cid = _X_CONTENT_MAP.get(_val(row, "tweet_id"))
        if cid is None:
            stats.skip("x", "scores")
            continue
        computed_at = _norm(_val(row, "computed_at")) or timeutil.iso_now()
        axes = {
            "likes_at_6h": _val(row, "likes_at_6h"),
            "replies_at_6h": _val(row, "replies_at_6h"),
            "score_engage": _val(row, "score_engage"),
            "score_spread": _val(row, "score_spread"),
            "score_first": _val(row, "score_first"),
        }
        legacy_story = _val(row, "story_id")
        storage.upsert_score(
            target, cid, computed_at,
            significance=_val(row, "significance"),
            branch=_norm_or_none(_val(row, "branch")),
            engagement=_val(row, "engagement"),
            velocity=_val(row, "velocity"),
            spread=_val(row, "spread"),
            xconf=_val(row, "xconf"),
            metrics_missing=_val(row, "metrics_missing") or 0,
            metrics_at=_norm(_val(row, "metrics_at")),
            metrics_age_hours=_val(row, "metrics_age_hours"),
            story_id=_X_STORY_MAP.get(legacy_story),
            axes_json=storage.jdump(axes),
        )
        storage.set_legacy_map(target, "x", "scores", _val(row, "tweet_id"), "score", cid)
        stats.add("scores", "score", "x")


def _migrate_x_classified(target, src, stats):
    for row in legacy.rows(src, "classified", order_by="text_hash"):
        stats.seen("x", "classified")
        text_hash = _val(row, "text_hash")
        fields = dict(
            is_ai=_val(row, "is_ai"),
            topic=_norm_or_none(_val(row, "topic")),
            subtopic=_norm_or_none(_val(row, "subtopic")),
            claim_type=_norm_or_none(_val(row, "claim_type")),
            novelty=_val(row, "novelty"),
            lang=_norm_or_none(_val(row, "lang")),
            method=_norm_or_none(_val(row, "method")),
            model=_norm_or_none(_val(row, "model")),
            status=_norm_or_none(_val(row, "status")),
            attempts=_val(row, "attempts") or 0,
            error=_val(row, "error"),
            prompt_tokens=_val(row, "prompt_tokens"),
            completion_tokens=_val(row, "completion_tokens"),
            cost_usd=_val(row, "cost_usd"),
            classified_at=_norm(_val(row, "classified_at")),
            first_seen_at=_norm(_val(row, "first_seen_at")),
        )
        storage.set_classify_cache(target, text_hash, **fields)
        # Проекция в classification для тех постов, что есть в content;
        # маппинг tweet_id→content_id уже записан из posts.
        cid = _X_CONTENT_MAP.get(_val(row, "tweet_id"))
        if cid is not None:
            storage.set_classification(target, cid, **fields)
        storage.set_legacy_map(target, "x", "classified", text_hash, "classify_cache", _NO_INT_TARGET)
        stats.add("classified", "classify_cache", "x")


def _migrate_x_classify_daily(target, src, stats):
    # TODO(debt-D-04): закрыт в ТЗ-1b — таблица classify_daily — см. TECH-DEBT.md.
    # D-04 закрыт: дневная статистика LLM-классификации X переносится в ядровую
    # таблицу ``classify_daily`` (колонки как в legacy + ``platform``).
    if not legacy.has_table(src, "classify_daily"):
        return
    for row in legacy.rows(src, "classify_daily", order_by="day"):
        stats.seen("x", "classify_daily")
        day = _norm_or_none(_val(row, "day"))
        storage.upsert_classify_daily(
            target, day, "x",
            posts=_val(row, "posts") or 0,
            model_calls=_val(row, "model_calls") or 0,
            failed=_val(row, "failed") or 0,
            prompt_tokens=_val(row, "prompt_tokens") or 0,
            completion_tokens=_val(row, "completion_tokens") or 0,
            cost_usd=_val(row, "cost_usd") or 0,
        )
        storage.set_legacy_map(target, "x", "classify_daily", day, "classify_daily", f"x|{day}")
        stats.add("classify_daily", "classify_daily", "x")


def _migrate_x_candidates(target, src, stats):
    # TODO(debt-D-06): закрыт в ТЗ-1b — candidate.meta_json — см. TECH-DEBT.md.
    for row in legacy.rows(src, "candidates", order_by="handle"):
        stats.seen("x", "candidates")
        handle = _val(row, "handle")
        # D-01/D-06 закрыты: человекочитаемый handle — в display_handle (ключом
        # остаётся handle), остатки X-полей (verified_at, sources) — в meta_json,
        # а не в sources_json.
        extra = {
            "verified_at": _norm(_val(row, "verified_at")),
            "sources": _norm_or_none(_val(row, "sources")),
        }
        cid = storage.add_candidate(
            target, "x", handle,
            kind="handle",
            display_handle=_display_handle(handle),
            found_via=_norm_or_none(_val(row, "found_via")) or _norm_or_none(_val(row, "feed_source")),
            found_in_handle=_norm_or_none(_val(row, "found_in_account")),
            score_priority=_val(row, "priority"),
            seen_count=_val(row, "seen_count") or 1,
            distinct_sources=_val(row, "distinct_sources") or 1,
            meta_json=storage.jdump(extra),
            first_seen_at=_norm(_val(row, "first_seen_at")),
            last_seen_at=_norm(_val(row, "last_seen_at")),
            validated=_norm_or_none(_val(row, "validated")),
            reject_reason=_norm_or_none(_val(row, "reject_reason")),
            llm_checked=_val(row, "llm_checked") or 0,
            ai_hint=_val(row, "ai_hint") or 0,
            spam=_val(row, "spam") or 0,
            lang_guess=_norm_or_none(_val(row, "lang_guess")),
            rubric=_norm_or_none(_val(row, "rubric")),
            promoted_by=_norm_or_none(_val(row, "promoted_by")),
            status="new",
        )
        storage.set_legacy_map(target, "x", "candidates", handle, "candidate", cid)
        stats.add("candidates", "candidate", "x")


def _migrate_x_transport(target, src, stats):
    # TODO(debt-D-05): закрыт в ТЗ-1b — cursor(account+search) — см. TECH-DEBT.md.
    global _X_RUN_MAP
    _X_RUN_MAP = {}
    # runs нужно раньше requests/run_log, чтобы резолвить run_id
    _migrate_x_runs_raw(target, src, stats)

    if legacy.has_table(src, "instances"):
        for row in legacy.rows(src, "instances", order_by="host"):
            stats.seen("x", "instances")
            host = _val(row, "host")
            meta = {
                "collect_fail_streak": _val(row, "collect_fail_streak"),
                "reserve_since": _norm(_val(row, "reserve_since")),
            }
            storage.upsert_transport_instance(
                target, "x", host,
                healthy=_val(row, "healthy"),
                rss_ok=_val(row, "rss_ok"),
                items_last_test=_val(row, "items_last_test"),
                last_check_at=_norm(_val(row, "last_check_at")),
                fail_streak=_val(row, "fail_streak") or 0,
                cooldown_until=_norm(_val(row, "cooldown_until")),
                requests_today=_val(row, "requests_today"),
                day=_norm_or_none(_val(row, "day")),
                version=_norm_or_none(_val(row, "version")),
                last_error=_val(row, "last_error"),
                rate_limited_429=_val(row, "rate_limited_429") or 0,
                blocked=_val(row, "blocked") or 0,
                meta_json=storage.jdump(meta),
            )
            storage.set_legacy_map(target, "x", "instances", host, "transport_instance", _NO_INT_TARGET)
            stats.add("instances", "transport_instance", "x")

    if legacy.has_table(src, "requests"):
        for row in legacy.rows(src, "requests", order_by="id"):
            stats.seen("x", "requests")
            old_id = _val(row, "id")
            if storage.get_legacy_map(target, "x", "requests", old_id) is not None:
                continue
            new_id = storage.add_transport_request(
                target, "x",
                host=_norm_or_none(_val(row, "host")),
                ts=_norm(_val(row, "ts")),
                kind=_norm_or_none(_val(row, "kind")),
                url=_val(row, "url"),
                status=_val(row, "status"),
                items=_val(row, "items"),
                latency_ms=_val(row, "latency_ms"),
                run_id=_X_RUN_MAP.get(_val(row, "run_id")),
            )
            storage.set_legacy_map(target, "x", "requests", old_id, "transport_request", new_id)
            stats.add("requests", "transport_request", "x")

    if legacy.has_table(src, "run_log"):
        for row in legacy.rows(src, "run_log", order_by="id"):
            stats.seen("x", "run_log")
            old_id = _val(row, "id")
            if storage.get_legacy_map(target, "x", "run_log", old_id) is not None:
                continue
            new_id = storage.log_run(
                target,
                _X_RUN_MAP.get(_val(row, "run_id")),
                _norm(_val(row, "ts")),
                _norm_or_none(_val(row, "level")),
                _norm_or_none(_val(row, "handle")),
                _val(row, "msg"),
                platform="x",
            )
            storage.set_legacy_map(target, "x", "run_log", old_id, "run_log", new_id)
            stats.add("run_log", "run_log", "x")

    # darks → source.meta_json.darks (status не меняем)
    if legacy.has_table(src, "darks"):
        for row in legacy.rows(src, "darks", order_by="handle"):
            stats.seen("x", "darks")
            sid = _X_SOURCE_BY_HANDLE.get(_val(row, "handle"))
            if sid is None:
                stats.skip("x", "darks")
                continue
            storage.merge_source_meta(target, sid, {"darks": {
                "computed_at": _norm(_val(row, "computed_at")),
                "stories_cur": _val(row, "stories_cur"),
                "stories_prev": _val(row, "stories_prev"),
                "growth": _val(row, "growth"),
                "in_top": _val(row, "in_top"),
            }})
            storage.set_legacy_map(target, "x", "darks", _val(row, "handle"), "source", sid)
            stats.add("darks", "source", "x")

    # D-05 закрыт: ВСЕ курсоры X (account и search) переносятся в ядровую
    # таблицу ``cursor`` (канонический источник). Курсоры лент дополнительно
    # дублируются в ``source.cursor`` — для уже работающего кода это не ломается,
    # но каноном объявлен ``cursor`` (см. SCHEMA.md).
    if legacy.has_table(src, "cursors"):
        for row in legacy.rows(src, "cursors", order_by="id"):
            stats.seen("x", "cursors")
            kind = _norm_or_none(_val(row, "kind")) or ""
            ref = _val(row, "ref")
            storage.upsert_cursor(
                target, "x", kind, ref,
                cursor=_val(row, "cursor"),
                last_page_at=_norm(_val(row, "last_page_at")),
                pages_total=_val(row, "pages_total"),
                items_total=_val(row, "items_total"),
                updated_at=_norm(_val(row, "last_page_at")),
            )
            storage.set_legacy_map(
                target, "x", "cursors", _val(row, "id"), "cursor",
                f"x|{kind}|{ref}",
            )
            stats.add("cursors", "cursor", "x")
            # Курсор ленты по-прежнему доступен через source.cursor.
            if kind == "account":
                sid = _X_SOURCE_BY_HANDLE.get(ref)
                if sid is None:
                    stats.skip("x", "cursors_account_source")
                    continue
                storage.set_source_cursor(
                    target, sid, _val(row, "cursor"),
                    patch={"cursor_pages_total": _val(row, "pages_total"),
                           "cursor_items_total": _val(row, "items_total"),
                           "cursor_last_page_at": _norm(_val(row, "last_page_at"))},
                )
                stats.add("cursors", "source", "x")


def _migrate_x_runs_raw(target, src, stats):
    if not legacy.has_table(src, "runs"):
        return
    for row in legacy.rows(src, "runs", order_by="id"):
        stats.seen("x", "runs")
        old_id = _val(row, "id")
        if storage.get_legacy_map(target, "x", "runs", old_id) is not None:
            _X_RUN_MAP[old_id] = storage.get_legacy_map(target, "x", "runs", old_id)
            continue
        new_id = storage.add_run(
            target, "x",
            started_at=_norm(_val(row, "started_at")),
            finished_at=_norm(_val(row, "finished_at")),
            mode=_norm_or_none(_val(row, "mode")),
            ok_count=_val(row, "accounts_ok"),
            fail_count=_val(row, "accounts_fail"),
            items_new=_val(row, "posts_new"),
            items_upd=_val(row, "posts_upd"),
            errors=_val(row, "errors"),
            note=_val(row, "note"),
        )
        _X_RUN_MAP[old_id] = new_id
        storage.set_legacy_map(target, "x", "runs", old_id, "run", new_id)
        stats.add("runs", "run", "x")


def _migrate_x_runs(target, src, stats):
    # Уже сделан в _migrate_x_runs_raw до requests/run_log.
    return


def _migrate_x_misc(target, src, stats):
    if legacy.has_table(src, "metrics_daily"):
        for row in legacy.rows(src, "metrics_daily", order_by="day"):
            stats.seen("x", "metrics_daily")
            day = _norm_or_none(_val(row, "day"))
            extra = {
                "cdn_429_count": _val(row, "cdn_429_count"),
                "synd_429_count": _val(row, "synd_429_count"),
                "ssr_used": _val(row, "ssr_used"),
                "stale_lag_p95_min": _val(row, "stale_lag_p95_min"),
            }
            storage.upsert_metrics_daily(
                target, day, "x",
                items_ingested=_val(row, "posts_ingested"),
                dup_rate=_val(row, "dup_rate"),
                coverage=_val(row, "coverage"),
                fail_rate=_val(row, "fail_rate"),
                latency_p95_min=_val(row, "latency_p95_min"),
                valid_date_ratio=_val(row, "valid_date_ratio"),
                instances_alive=_val(row, "instances_alive"),
                likes_median=_val(row, "likes_median"),
                enriched_ratio=int(_val(row, "enriched_ratio") or 0) if _val(row, "enriched_ratio") is not None else None,
                extra_json=storage.jdump(extra),
            )
            storage.set_legacy_map(target, "x", "metrics_daily", day, "metrics_daily", _NO_INT_TARGET)
            stats.add("metrics_daily", "metrics_daily", "x")

    if legacy.has_table(src, "blocklist"):
        for row in legacy.rows(src, "blocklist", order_by="handle"):
            stats.seen("x", "blocklist")
            storage.upsert_blocklist(
                target, "x", _val(row, "handle"),
                reason=_val(row, "reason"), added_at=_norm(_val(row, "added_at")),
            )
            storage.set_legacy_map(target, "x", "blocklist", _val(row, "handle"), "blocklist", _NO_INT_TARGET)
            stats.add("blocklist", "blocklist", "x")

    if legacy.has_table(src, "report_texts"):
        for row in legacy.rows(src, "report_texts", order_by="text_hash"):
            stats.seen("x", "report_texts")
            storage.upsert_report_text(
                target, _val(row, "text_hash"),
                ru=_val(row, "ru"), model=_norm_or_none(_val(row, "model")),
                created_at=_norm(_val(row, "created_at")), src=_norm_or_none(_val(row, "src")),
            )
            storage.set_legacy_map(target, "x", "report_texts", _val(row, "text_hash"), "report_text", _NO_INT_TARGET)
            stats.add("report_texts", "report_text", "x")


# ---------------------------------------------------------------------------
# tuber-telegram
# ---------------------------------------------------------------------------

def migrate_tg(target: sqlite3.Connection, path: str, stats: Stats) -> None:
    src = legacy.open_legacy(path)
    try:
        with db.write_tx(target):
            _migrate_tg_sources(target, src, stats)
            _migrate_tg_content(target, src, stats)
            _migrate_tg_scores(target, src, stats)
            _migrate_tg_baselines(target, src, stats)
            _migrate_tg_stories(target, src, stats)
            _migrate_tg_classified(target, src, stats)
            _migrate_tg_runs(target, src, stats)
            _migrate_tg_misc(target, src, stats)
    finally:
        src.close()


def _migrate_tg_sources(target, src, stats):
    global _TG_SOURCE_MAP, _TG_HANDLE_MAP, _TG_SID_HANDLE
    _TG_SOURCE_MAP = {}
    _TG_HANDLE_MAP = {}
    _TG_SID_HANDLE = {}
    for row in legacy.rows(src, "channels", order_by="id"):
        stats.seen("tg", "channels")
        handle = _val(row, "handle")
        tg_id = _val(row, "tg_id")
        meta = {
            "posts_7d": _val(row, "posts_7d"),
            "last_post_at": _norm(_val(row, "last_post_at")),
        }
        sid = storage.upsert_source(
            target, "telegram", handle,
            external_id=str(tg_id) if tg_id is not None else None,
            title=_norm_or_none(_val(row, "title")),
            subs=_val(row, "subs"),
            subs_at=_norm(_val(row, "subs_at")),
            avg_views=_val(row, "avg_views"),
            vr=_val(row, "vr"),
            lang=_norm_or_none(_val(row, "lang")),
            topic_guess=_norm_or_none(_val(row, "topic_guess")),
            status=_norm_or_none(_val(row, "status")),
            read_mode=_norm_or_none(_val(row, "read_mode")),
            is_author=_val(row, "is_author"),
            antifraud_flag=_val(row, "antifraud_flag") or 0,
            flood_until=_norm(_val(row, "flood_until")),
            added_at=_norm(_val(row, "added_at")),
            checked_at=_norm(_val(row, "checked_at")),
            source_kind=_norm_or_none(_val(row, "source")),
            notes=_val(row, "notes"),
            first_seen_at=_norm(_val(row, "added_at")),
            last_synced_at=_norm(_val(row, "checked_at")),
            meta_json=storage.jdump(meta),
        )
        _TG_SOURCE_MAP[_val(row, "id")] = sid
        _TG_HANDLE_MAP[handle] = sid
        _TG_SID_HANDLE[sid] = handle
        storage.set_legacy_map(target, "tg", "channels", _val(row, "id"), "source", sid)
        stats.add("channels", "source", "tg")


def _migrate_tg_content(target, src, stats):
    global _TG_CONTENT_MAP
    _TG_CONTENT_MAP = {}
    for row in legacy.rows(src, "posts", order_by="id"):
        stats.seen("tg", "posts")
        post_id = _val(row, "id")
        channel_id = _val(row, "channel_id")
        sid = _TG_SOURCE_MAP.get(channel_id)
        handle = _TG_SID_HANDLE.get(sid)
        if handle is None:
            handle = str(channel_id)
        message_id = _val(row, "message_id")
        ext = ids.tg_external_id(handle, message_id)
        meta = {
            "message_id": message_id,
            "fwd_from": _norm_or_none(_val(row, "fwd_from")),
            "has_own_media": _val(row, "has_own_media"),
            "views": _val(row, "views"),
            "forwards": _val(row, "forwards"),
            "reactions": _val(row, "reactions"),
            "views_checked_at": _norm(_val(row, "views_checked_at")),
        }
        # TODO(debt-D-45): закрыт в ТЗ-6 — content.url синтезирует общий
        # ядровой хелпер tuber.core.urls внутри storage.upsert_content.
        cid = storage.upsert_content(
            target, "telegram", ext,
            source_id=sid,
            kind="post",
            text=_val(row, "text"),
            text_hash=_norm_or_none(_val(row, "text_hash")),
            published_at=_norm(_val(row, "date_utc")),
            media_kind=_norm_or_none(_val(row, "media_kind")),
            links=_norm_or_none(_val(row, "links")),
            hashtags=_norm_or_none(_val(row, "hashtags")),
            mentions=_norm_or_none(_val(row, "mentions")),
            is_repost=_val(row, "is_forward") or 0,
            is_promo=_val(row, "is_ad") or 0,
            first_seen_at=_norm(_val(row, "first_seen_at")),
            meta_json=storage.jdump(meta),
        )
        _TG_CONTENT_MAP[post_id] = cid
        captured_at = (
            _norm(_val(row, "views_checked_at"))
            or _norm(_val(row, "first_seen_at"))
            or _norm(_val(row, "date_utc"))
        )
        if captured_at is not None:
            storage.add_snapshot(
                target, cid, captured_at,
                views=_val(row, "views"),
                forwards=_val(row, "forwards"),
                reactions=_val(row, "reactions"),
                source="telegram",
            )
            storage.set_content_latest(
                target, cid,
                captured_at=captured_at,
                views=_val(row, "views"),
                forwards=_val(row, "forwards"),
                reactions=_val(row, "reactions"),
            )
        storage.set_legacy_map(target, "tg", "posts", post_id, "content", cid)
        stats.add("posts", "content", "tg")


def _migrate_tg_scores(target, src, stats):
    for row in legacy.rows(src, "scores", order_by="post_id"):
        stats.seen("tg", "scores")
        cid = _TG_CONTENT_MAP.get(_val(row, "post_id"))
        if cid is None:
            stats.skip("tg", "scores")
            continue
        computed_at = _norm(_val(row, "computed_at")) or timeutil.iso_now()
        axes = {
            "er": _val(row, "er"),
            "eng_channel": _val(row, "eng_channel"),
            "eng_global": _val(row, "eng_global"),
            "wsrc": _val(row, "wsrc"),
            "dup_penalty": _val(row, "dup_penalty"),
            "fr": _val(row, "fr"),
            "topic_weight": _val(row, "topic_weight"),
            "age_days": _val(row, "age_days"),
        }
        storage.upsert_score(
            target, cid, computed_at,
            significance=_val(row, "significance"),
            engagement=_val(row, "eng"),
            decay=_val(row, "decay"),
            xconf=_val(row, "xconf"),
            anomaly=_val(row, "anomaly") or 0,
            axes_json=storage.jdump(axes),
        )
        storage.set_legacy_map(target, "tg", "scores", _val(row, "post_id"), "score", cid)
        stats.add("scores", "score", "tg")


def _migrate_tg_baselines(target, src, stats):
    for row in legacy.rows(src, "channel_baselines", order_by="channel_id"):
        stats.seen("tg", "channel_baselines")
        sid = _TG_SOURCE_MAP.get(_val(row, "channel_id"))
        if sid is None:
            stats.skip("tg", "channel_baselines")
            continue
        storage.upsert_source_baseline(
            target, sid,
            window_days=_val(row, "window_days"),
            items_in_window=_val(row, "posts_in_window"),
            median_views=_val(row, "median_views"),
            median_reactions=_val(row, "median_reactions"),
            median_er=_val(row, "median_er"),
            is_author_data=_val(row, "is_author_data"),
            hashed_items=_val(row, "hashed_posts"),
            dup_items=_val(row, "dup_posts"),
            dup_ratio=_val(row, "dup_ratio"),
            computed_at=_norm(_val(row, "computed_at")),
        )
        storage.set_legacy_map(target, "tg", "channel_baselines", _val(row, "channel_id"), "source_baseline", sid)
        stats.add("channel_baselines", "source_baseline", "tg")


def _migrate_tg_stories(target, src, stats):
    global _TG_STORY_MAP
    _TG_STORY_MAP = {}
    if legacy.has_table(src, "stories"):
        for row in legacy.rows(src, "stories", order_by="id"):
            stats.seen("tg", "stories")
            old_id = _val(row, "id")
            mapped = storage.get_legacy_map(target, "tg", "stories", old_id)
            if mapped is not None:
                _TG_STORY_MAP[old_id] = mapped
                continue
            new_id = storage.add_story(
                target,
                platform="telegram",
                created_at=_norm(_val(row, "created_at")),
                title=_norm_or_none(_val(row, "title")),
                topic=_norm_or_none(_val(row, "topic")),
                canonical_content_id=_TG_CONTENT_MAP.get(_val(row, "canonical_post_id")),
                source_count=_val(row, "channel_count") or 0,
                xconf=_val(row, "xconf") or 0,
                first_pub_at=_norm(_val(row, "first_pub_at")),
                last_pub_at=_norm(_val(row, "last_pub_at")),
                significance=_val(row, "significance"),
                rank_score=_val(row, "rank_score"),
            )
            _TG_STORY_MAP[old_id] = new_id
            storage.set_legacy_map(target, "tg", "stories", old_id, "story", new_id)
            stats.add("stories", "story", "tg")
    if legacy.has_table(src, "story_members"):
        for row in legacy.rows(src, "story_members", order_by="story_id, post_id"):
            stats.seen("tg", "story_members")
            story_id = _TG_STORY_MAP.get(_val(row, "story_id"))
            cid = _TG_CONTENT_MAP.get(_val(row, "post_id"))
            if story_id is None or cid is None:
                stats.skip("tg", "story_members")
                continue
            storage.add_story_member(
                target, story_id, cid,
                role=_norm_or_none(_val(row, "role")),
                sim=_val(row, "sim"),
                is_canonical=_val(row, "is_canonical") or 0,
            )
            storage.set_legacy_map(
                target, "tg", "story_members",
                f"{_val(row, 'story_id')}|{_val(row, 'post_id')}", "story_member", cid,
            )
            stats.add("story_members", "story_member", "tg")


def _migrate_tg_classified(target, src, stats):
    if not legacy.has_table(src, "classified"):
        return
    for row in legacy.rows(src, "classified", order_by="post_id"):
        stats.seen("tg", "classified")
        cid = _TG_CONTENT_MAP.get(_val(row, "post_id"))
        if cid is None:
            stats.skip("tg", "classified")
            continue
        storage.set_classification(
            target, cid,
            is_ai=_val(row, "is_ai"),
            topic=_norm_or_none(_val(row, "topic")),
            source_type=_norm_or_none(_val(row, "source_type")),
            claim_type=_norm_or_none(_val(row, "claim_type")),
            entity_tier=_norm_or_none(_val(row, "entity_tier")),
            novelty=_val(row, "novelty"),
            lang=_norm_or_none(_val(row, "lang")),
            confidence=_val(row, "confidence"),
            model=_norm_or_none(_val(row, "model")),
            prompt_ver=_norm_or_none(_val(row, "prompt_ver")),
            classified_at=_norm(_val(row, "classified_at")),
        )
        storage.set_legacy_map(target, "tg", "classified", _val(row, "post_id"), "classification", cid)
        stats.add("classified", "classification", "tg")


def _migrate_tg_runs(target, src, stats):
    global _TG_RUN_MAP
    _TG_RUN_MAP = {}
    if legacy.has_table(src, "runs"):
        for row in legacy.rows(src, "runs", order_by="id"):
            stats.seen("tg", "runs")
            old_id = _val(row, "id")
            mapped = storage.get_legacy_map(target, "tg", "runs", old_id)
            if mapped is not None:
                _TG_RUN_MAP[old_id] = mapped
                continue
            new_id = storage.add_run(
                target, "telegram",
                started_at=_norm(_val(row, "started_at")),
                finished_at=_norm(_val(row, "finished_at")),
                mode=_norm_or_none(_val(row, "mode")),
                ok_count=_val(row, "channels_ok"),
                fail_count=_val(row, "channels_fail"),
                items_new=_val(row, "posts_new"),
                items_upd=_val(row, "posts_upd"),
                errors=_val(row, "errors"),
                note=_val(row, "note"),
            )
            _TG_RUN_MAP[old_id] = new_id
            storage.set_legacy_map(target, "tg", "runs", old_id, "run", new_id)
            stats.add("runs", "run", "tg")
    if legacy.has_table(src, "run_log"):
        for row in legacy.rows(src, "run_log", order_by="id"):
            stats.seen("tg", "run_log")
            old_id = _val(row, "id")
            if storage.get_legacy_map(target, "tg", "run_log", old_id) is not None:
                continue
            new_id = storage.log_run(
                target,
                _TG_RUN_MAP.get(_val(row, "run_id")),
                _norm(_val(row, "ts")),
                _norm_or_none(_val(row, "level")),
                _norm_or_none(_val(row, "handle")),
                _val(row, "msg"),
                platform="telegram",
            )
            storage.set_legacy_map(target, "tg", "run_log", old_id, "run_log", new_id)
            stats.add("run_log", "run_log", "tg")


def _migrate_tg_misc(target, src, stats):
    if legacy.has_table(src, "metrics_daily"):
        for row in legacy.rows(src, "metrics_daily", order_by="day"):
            stats.seen("tg", "metrics_daily")
            day = _norm_or_none(_val(row, "day"))
            storage.upsert_metrics_daily(
                target, day, "telegram",
                items_ingested=_val(row, "posts_ingested"),
                dup_rate=_val(row, "dup_rate"),
                coverage=_val(row, "coverage_est"),
                latency_p95_min=_val(row, "latency_p90_min"),
                enriched_ratio=_val(row, "enrich_cov"),
                errors=_val(row, "errors"),
            )
            storage.set_legacy_map(target, "tg", "metrics_daily", day, "metrics_daily", _NO_INT_TARGET)
            stats.add("metrics_daily", "metrics_daily", "tg")

    if legacy.has_table(src, "account_state"):
        for row in legacy.rows(src, "account_state", order_by="name"):
            stats.seen("tg", "account_state")
            storage.upsert_transport_account_state(
                target, "telegram", _val(row, "name"),
                flood_until=_norm(_val(row, "flood_until")),
                last_error=_val(row, "last_error"),
                resolves_today=_val(row, "resolves_today") or 0,
                day=_norm_or_none(_val(row, "day")),
                updated_at=_norm(_val(row, "updated_at")),
            )
            storage.set_legacy_map(target, "tg", "account_state", _val(row, "name"), "transport_account_state", _NO_INT_TARGET)
            stats.add("account_state", "transport_account_state", "tg")

    # D-05: если у Telegram тоже есть курсоры — переносим их в ``cursor``.
    if legacy.has_table(src, "cursors"):
        for row in legacy.rows(src, "cursors", order_by="id"):
            stats.seen("tg", "cursors")
            kind = _norm_or_none(_val(row, "kind")) or ""
            ref = _val(row, "ref")
            storage.upsert_cursor(
                target, "telegram", kind, ref,
                cursor=_val(row, "cursor"),
                last_page_at=_norm(_val(row, "last_page_at")),
                pages_total=_val(row, "pages_total"),
                items_total=_val(row, "items_total"),
                updated_at=_norm(_val(row, "last_page_at")),
            )
            storage.set_legacy_map(
                target, "tg", "cursors", _val(row, "id"), "cursor",
                f"telegram|{kind}|{ref}",
            )
            stats.add("cursors", "cursor", "tg")


# ---------------------------------------------------------------------------
# Оркестрация
# ---------------------------------------------------------------------------

def _record_schema_work(stats: Stats, target: sqlite3.Connection,
                        schema_report: dict[str, object]) -> None:
    """Записать в ``stats`` фактическую работу по схеме (ТЗ-3d).

    Счётчики берутся из отчёта :func:`tuber.core.schema.migrate_schema` (diff
    ``sqlite_master`` до/после) и из :func:`tuber.core.schema.verify_schema` /
    :func:`tuber.core.schema.duplicate_index_report` — то есть из состояния
    базы, а не из предположений о ней.
    """
    stats.created_tables = [str(n) for n in (schema_report.get("created_tables") or [])]
    stats.created_indexes = [str(n) for n in (schema_report.get("created_indexes") or [])]
    stats.created_triggers = [str(n) for n in (schema_report.get("created_triggers") or [])]
    stats.added_columns = [str(n) for n in (schema_report.get("added_columns") or [])]
    denorm = schema_report.get("denormalized")
    stats.denorm_counts = {k: int(v) for k, v in denorm.items()} if isinstance(
        denorm, dict) else {}
    stats.schema_problems_after = verify_schema(target)
    stats.duplicate_problems = duplicate_index_report(target)


def migrate(
    target_path: str,
    *,
    os_path: str | None = None,
    x_path: str | None = None,
    tg_path: str | None = None,
    recompute_latest: bool = True,
    analyze: bool = True,
    vacuum: bool | None = None,
) -> Stats:
    """Выполнить миграцию. Возвращает статистику.

    ``analyze`` (по умолчанию) — после загрузки прогнать ``ANALYZE``: без
    ``sqlite_stat1`` планировщик оценивает соединения «на глаз» и отчёт
    YouTube деградирует в разы (см. TECH-DEBT D-21). Статистика нужна ровно
    один раз, после массовой вставки; в кроне её гонять не надо.

    ``vacuum`` (ТЗ-3d) — вернуть файлу место, освобождённое удалением лишних
    индексов: ``DROP INDEX`` лишь помещает страницы в freelist, размер файла
    не уменьшается. По умолчанию (``None``) ``VACUUM`` выполняется ТОЛЬКО когда
    на этом прогоне реально что-то удалено (то есть один раз на базе) — на
    идемпотентном повторе он не нужен и не гоняется.
    """
    stats = Stats()
    target = db.connect(target_path)
    try:
        # Проблемы ДО — чтобы сводка на идемпотентном повторе (когда новых строк
        # нет и печатается схема) показывала результат, а не пустые нули.
        stats.schema_problems_before = verify_schema(target)
        with db.write_tx(target):
            schema_report = migrate_schema(target, backfill_url=True)
        dropped = schema_report.get("dropped_indexes") or []
        stats.dropped_indexes = list(dropped)
        _record_schema_work(stats, target, schema_report)
        if os_path:
            migrate_os(target, os_path, stats)
        if x_path:
            migrate_x(target, x_path, stats)
        if tg_path:
            migrate_tg(target, tg_path, stats)
        if recompute_latest:
            with db.write_tx(target):
                storage.recompute_content_latest(target)
        if analyze:
            with db.write_tx(target):
                target.execute("ANALYZE")
            stats.analyzed = True
        do_vacuum = bool(dropped) if vacuum is None else bool(vacuum)
        if do_vacuum:
            # VACUUM нельзя выполнять внутри транзакции; write_tx уже закрыт.
            target.execute("VACUUM")
            stats.vacuumed = True
    finally:
        target.close()
    return stats


def migrate_schema_only(
    target_path: str,
    *,
    analyze: bool = False,
    vacuum: bool | None = None,
) -> Stats:
    """Привести схему УЖЕ СОБРАННОЙ базы к текущей, не перенося данные (ТЗ-3d).

    Это рабочий путь чистки боевых баз: ``migrate_schema`` идемпотентно
    достраивает недостающие ядровые индексы и снимает лишние адаптерные
    (``REDUNDANT_ADAPTER_INDEXES``), а ``VACUUM`` возвращает файлу место — сам
    ``DROP INDEX`` только помещает страницы в freelist и размер файла не меняет.

    ``ANALYZE`` по умолчанию НЕ гоняется: удалённые индексы меняют статистику,
    но на большой базе ``ANALYZE`` дорог, и полноценный пересчёт делается
    отдельным осознанным шагом (см. D-21).

    ``vacuum=None`` — ``VACUUM`` только если на этом прогоне что-то удалено
    (то есть один раз на базе; идемпотентный повтор место не трогает).
    """
    stats = Stats()
    stats.schema_only = True
    target = db.connect(target_path)
    try:
        # Проблемы ДО считаем до транзакции: сводка обязана показать, что прогон
        # реально исправил, а не только что база «уже чистая» (ТЗ-3d).
        stats.schema_problems_before = verify_schema(target)
        with db.write_tx(target):
            report = migrate_schema(target, backfill_url=True)
        dropped = report.get("dropped_indexes")
        stats.dropped_indexes = [str(n) for n in dropped] if isinstance(dropped, list) else []
        _record_schema_work(stats, target, report)
        if analyze:
            with db.write_tx(target):
                target.execute("ANALYZE")
            stats.analyzed = True
        do_vacuum = bool(stats.dropped_indexes) if vacuum is None else bool(vacuum)
        if do_vacuum:
            # VACUUM нельзя выполнять внутри транзакции; write_tx уже закрыт.
            target.execute("VACUUM")
            stats.vacuumed = True
    finally:
        target.close()
    return stats


def format_summary(stats: Stats) -> str:
    """Сводка «legacy → target: сколько строк» (или чистки схемы, ТЗ-3d)."""
    if stats.schema_only or (not stats.migrated
                             and (stats.dropped_indexes or stats.vacuumed or stats.analyzed)):
        # Режим ``--schema-only`` (или идемпотентный повтор ``migrate``, когда
        # новых строк нет): отчитываемся о работе по схеме.
        # Сводка обязана показать СДЕЛАННУЮ РАБОТУ и её результат, иначе прогон,
        # исправивший 53 проблемы схемы, печатал «база уже чистая» (ТЗ-3d).
        lines = ["Сводка чистки схемы (ТЗ-3d, без переноса данных):", ""]

        dropped = sorted(stats.dropped_indexes)
        if dropped:
            lines.append("Удалено лишних индексов на таблицах ядра: " + ", ".join(dropped))
        elif stats.duplicate_problems:
            # Сняли 0, но проверка дублей НЕ чиста — не выдаём ложный «чисто».
            lines.append("ВНИМАНИЕ: на таблицах ядра остались дубли индексов: "
                         + "; ".join(stats.duplicate_problems))
        else:
            lines.append("Лишних индексов на таблицах ядра нет (проверка дублей пройдена).")

        created = (len(stats.created_tables) + len(stats.created_indexes)
                   + len(stats.created_triggers) + len(stats.added_columns))
        lines.append(
            f"Выполнено: создано таблиц {len(stats.created_tables)}, "
            f"индексов {len(stats.created_indexes)}, "
            f"триггеров {len(stats.created_triggers)}, "
            f"колонок {len(stats.added_columns)}; "
            f"снято индексов {len(dropped)}.")
        if created == 0 and not dropped:
            lines.append("Схема уже актуальна (делать нечего).")

        backfilled = sum(stats.denorm_counts.values())
        detail = ", ".join(
            f"{name} {n}" for name, n in sorted(stats.denorm_counts.items()) if n)
        lines.append("Дозаполнено строк бэкфиллом денормализации: "
                     + str(backfilled) + (f" ({detail})" if detail else ""))

        lines.append(f"Проблем схемы ядра: до {len(stats.schema_problems_before)}, "
                     f"после {len(stats.schema_problems_after)}.")
        lines.append("VACUUM: " + ("выполнен" if stats.vacuumed else "не выполнялся"))
        lines.append("ANALYZE: " + ("выполнен" if stats.analyzed else "не выполнялся"))
        return "\n".join(lines)

    lines = ["Сводка переноса (legacy → core):", ""]
    lines.append(f"{'legacy_db':<10} {'legacy_table':<22} {'target_table':<24} {'rows':>8}")
    lines.append("-" * 68)
    for (ldb, ltable, ttable), n in sorted(stats.migrated.items()):
        lines.append(f"{ldb:<10} {ltable:<22} {ttable:<24} {n:>8}")

    if stats.skipped:
        lines.append("")
        lines.append("Пропущено (нет родителя/битая дата):")
        for (ldb, ltable), n in sorted(stats.skipped.items()):
            lines.append(f"  {ldb}.{ltable}: {n}")
    if stats.dropped_indexes:
        lines.append("")
        lines.append("Удалено лишних индексов на таблицах ядра (ТЗ-3d): "
                     + ", ".join(sorted(stats.dropped_indexes)))
        if stats.vacuumed:
            lines.append("VACUUM: выполнен (место возвращено файлу).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuber migrate", description="Миграция legacy-баз в единое ядро")
    parser.add_argument("--target", required=True, help="путь к единой базе (будет создана/обновлена)")
    parser.add_argument("--os", dest="os_path", help="путь к tuber-os/data/tuber.db")
    parser.add_argument("--x", dest="x_path", help="путь к tuber-x/data/tuber_x.db")
    parser.add_argument("--tg", dest="tg_path", help="путь к tuber-telegram/data/tuber_telegram.db")
    parser.add_argument("--no-latest", action="store_true", help="не пересчитывать content_latest в конце")
    parser.add_argument("--no-analyze", action="store_true",
                        help="не обновлять статистику планировщика (ANALYZE) в конце")
    parser.add_argument("--vacuum", dest="vacuum", action="store_true",
                        help="всегда выполнять VACUUM в конце (по умолчанию — только если удалены лишние индексы)")
    parser.add_argument("--no-vacuum", dest="no_vacuum", action="store_true",
                        help="не выполнять VACUUM даже после удаления лишних индексов")
    parser.add_argument("--schema-only", dest="schema_only", action="store_true",
                        help="только привести схему УЖЕ СОБРАННОЙ базы к текущей "
                             "(достроить ядровые индексы, снять лишние адаптерные), "
                             "не перенося данные — источники в этом режиме не нужны")
    args = parser.parse_args(argv)

    if not args.schema_only and not any([args.os_path, args.x_path, args.tg_path]):
        parser.error("нужен хотя бы один источник: --os / --x / --tg (или --schema-only)")

    vacuum: bool | None = None
    if args.vacuum:
        vacuum = True
    elif args.no_vacuum:
        vacuum = False

    if args.schema_only:
        stats = migrate_schema_only(
            args.target,
            analyze=not args.no_analyze,
            vacuum=vacuum,
        )
        print(format_summary(stats))
        return 0

    stats = migrate(
        args.target,
        os_path=args.os_path,
        x_path=args.x_path,
        tg_path=args.tg_path,
        recompute_latest=not args.no_latest,
        analyze=not args.no_analyze,
        vacuum=vacuum,
    )
    print(format_summary(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
