"""Сверка чисел legacy ↔ ядро (parity, ТЗ-1 §6).

Инструмент по каждому правилу считает «было» (COUNT в legacy, read-only) и
«стало» (COUNT в ядре по явному правилу) и печатает таблицу
``таблица | было | стало | расхождение``.

Коды возврата:

* ``0`` — все правила сошлись (или расхождение объяснено в ``known_diffs``);
* ``2`` — есть хоть одно необъяснённое расхождение.

``known_diffs`` — список объяснимых расхождений (по умолчанию пуст: ноль
расхождений — норма, расхождение — повод разбираться).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from tuber.core import db, legacy


@dataclass(frozen=True)
class Rule:
    id: str
    legacy_table: str          # человекочитаемое имя legacy-таблицы
    legacy_db: str             # os | x | tg
    legacy_sql: str            # SQL по legacy (read-only)
    core_sql: str              # SQL по ядру
    note: str = ""             # пояснение правила


# Правила сверки. Порядок — как в §6 ТЗ-1.
RULES: tuple[Rule, ...] = (
    # --- tuber-os ---
    Rule("os.channels", "os.channels", "os",
         "SELECT COUNT(*) FROM channels",
         "SELECT COUNT(*) FROM source WHERE platform='youtube'"),
    Rule("os.videos", "os.videos", "os",
         "SELECT COUNT(*) FROM videos",
         "SELECT COUNT(*) FROM content WHERE platform='youtube' AND kind IN ('video','short')"),
    Rule("os.snapshots", "os.snapshots", "os",
         "SELECT COUNT(*) FROM snapshots",
         "SELECT COUNT(*) FROM metric_snapshot m JOIN content c ON c.id=m.content_id "
         "WHERE c.platform='youtube'"),
    Rule("os.video_scores", "os.video_scores", "os",
         "SELECT COUNT(*) FROM video_scores",
         "SELECT COUNT(*) FROM score s JOIN content c ON c.id=s.content_id WHERE c.platform='youtube'"),
    Rule("os.video_classification", "os.video_classification", "os",
         "SELECT COUNT(*) FROM video_classification",
         "SELECT COUNT(*) FROM classification cl JOIN content c ON c.id=cl.content_id "
         "WHERE c.platform='youtube'"),
    Rule("os.video_classification.title_ru", "os.video_classification", "os",
         "SELECT COUNT(*) FROM video_classification WHERE title_ru IS NOT NULL",
         "SELECT COUNT(*) FROM classification cl JOIN content c ON c.id=cl.content_id "
         "WHERE c.platform='youtube' AND cl.title_ru IS NOT NULL",
         note="D-03: непустой title_ru не теряется"),
    Rule("os.video_classification.summary_ru", "os.video_classification", "os",
         "SELECT COUNT(*) FROM video_classification WHERE summary_ru IS NOT NULL",
         "SELECT COUNT(*) FROM classification cl JOIN content c ON c.id=cl.content_id "
         "WHERE c.platform='youtube' AND cl.summary_ru IS NOT NULL",
         note="D-03: непустой summary_ru не теряется"),
    Rule("os.video_classification.reason", "os.video_classification", "os",
         "SELECT COUNT(*) FROM video_classification WHERE reason IS NOT NULL",
         "SELECT COUNT(*) FROM classification cl JOIN content c ON c.id=cl.content_id "
         "WHERE c.platform='youtube' AND cl.reason IS NOT NULL",
         note="D-03: reason не теряется"),
    Rule("os.seo_fields", "os.seo_fields", "os",
         "SELECT COUNT(*) FROM seo_fields",
         "SELECT COUNT(*) FROM seo_field sf JOIN content c ON c.id=sf.content_id WHERE c.platform='youtube'"),
    Rule("os.quota_log", "os.quota_log", "os",
         "SELECT COALESCE(SUM(calls),0) FROM quota_log",
         "SELECT COALESCE(SUM(calls),0) FROM quota_usage WHERE platform='youtube'",
         note="сравнивается SUM(calls): строки агрегируются по (key_id, day, endpoint)"),
    Rule("os.llm_usage", "os.llm_usage", "os",
         "SELECT COUNT(*) FROM llm_usage",
         "SELECT COUNT(*) FROM llm_usage WHERE platform='youtube'"),
    Rule("os.channel_candidates+query_candidates", "os.channel_candidates+query_candidates", "os",
         "SELECT (SELECT COUNT(*) FROM channel_candidates)+(SELECT COUNT(*) FROM query_candidates)",
         "SELECT COUNT(*) FROM candidate WHERE platform='youtube'"),
    Rule("os.thumbnail_vision", "os.thumbnail_vision", "os",
         "SELECT COUNT(*) FROM thumbnail_vision",
         "SELECT COUNT(*) FROM thumbnail_vision tv JOIN content c ON c.id=tv.content_id "
         "WHERE c.platform='youtube'"),
    Rule("os.video_comments", "os.video_comments", "os",
         "SELECT COUNT(*) FROM video_comments",
         "SELECT COUNT(*) FROM content_comment WHERE platform='youtube'"),
    Rule("os.comment_checks", "os.comment_checks", "os",
         "SELECT COUNT(*) FROM comment_checks",
         "SELECT COUNT(*) FROM comment_check cc JOIN content c ON c.id=cc.content_id WHERE c.platform='youtube'"),
    Rule("os.topics", "os.topics", "os",
         "SELECT COUNT(*) FROM topics",
         "SELECT COUNT(*) FROM topic WHERE platform='youtube'"),
    # --- tuber-x ---
    Rule("x.accounts", "x.accounts", "x",
         "SELECT COUNT(*) FROM accounts",
         "SELECT COUNT(*) FROM source WHERE platform='x'"),
    Rule("x.posts", "x.posts", "x",
         "SELECT COUNT(*) FROM posts",
         "SELECT COUNT(*) FROM content WHERE platform='x'"),
    Rule("x.post_metrics_history", "x.post_metrics_history", "x",
         "SELECT COUNT(*) FROM post_metrics_history",
         "SELECT COUNT(*) FROM metric_snapshot m JOIN content c ON c.id=m.content_id WHERE c.platform='x'"),
    Rule("x.scores", "x.scores", "x",
         "SELECT COUNT(*) FROM scores",
         "SELECT COUNT(*) FROM score s JOIN content c ON c.id=s.content_id WHERE c.platform='x'"),
    Rule("x.stories", "x.stories", "x",
         "SELECT COUNT(*) FROM stories",
         "SELECT COUNT(*) FROM story WHERE platform='x'"),
    Rule("x.story_posts", "x.story_posts", "x",
         "SELECT COUNT(*) FROM story_posts",
         "SELECT COUNT(*) FROM story_member sm JOIN story s ON s.id=sm.story_id WHERE s.platform='x'"),
    Rule("x.classified", "x.classified", "x",
         "SELECT COUNT(*) FROM classified",
         "SELECT COUNT(*) FROM classify_cache"),
    Rule("x.classify_daily", "x.classify_daily", "x",
         "SELECT COUNT(*) FROM classify_daily",
         "SELECT COUNT(*) FROM classify_daily WHERE platform='x'",
         note="D-04: дневная статистика классификации X"),
    Rule("x.cursors", "x.cursors", "x",
         "SELECT COUNT(*) FROM cursors",
         "SELECT COUNT(*) FROM cursor WHERE platform='x'",
         note="D-05: переносятся и account-, и search-курсоры"),
    Rule("x.candidates", "x.candidates", "x",
         "SELECT COUNT(*) FROM candidates",
         "SELECT COUNT(*) FROM candidate WHERE platform='x'"),
    Rule("x.requests", "x.requests", "x",
         "SELECT COUNT(*) FROM requests",
         "SELECT COUNT(*) FROM transport_request WHERE platform='x'"),
    Rule("x.instances", "x.instances", "x",
         "SELECT COUNT(*) FROM instances",
         "SELECT COUNT(*) FROM transport_instance WHERE platform='x'"),
    Rule("x.runs", "x.runs", "x",
         "SELECT COUNT(*) FROM runs",
         "SELECT COUNT(*) FROM run WHERE platform='x'"),
    Rule("x.run_log", "x.run_log", "x",
         "SELECT COUNT(*) FROM run_log",
         "SELECT COUNT(*) FROM run_log WHERE platform='x'",
         note="D-24: принадлежность явной колонкой platform (без допущения «run_id IS NULL = X»)"),
    Rule("x.metrics_daily", "x.metrics_daily", "x",
         "SELECT COUNT(*) FROM metrics_daily",
         "SELECT COUNT(*) FROM metrics_daily WHERE platform='x'"),
    Rule("x.report_texts", "x.report_texts", "x",
         "SELECT COUNT(*) FROM report_texts",
         "SELECT COUNT(*) FROM report_text"),
    Rule("x.blocklist", "x.blocklist", "x",
         "SELECT COUNT(*) FROM blocklist",
         "SELECT COUNT(*) FROM blocklist WHERE platform='x'"),
    # --- tuber-telegram ---
    Rule("tg.channels", "tg.channels", "tg",
         "SELECT COUNT(*) FROM channels",
         "SELECT COUNT(*) FROM source WHERE platform='telegram'"),
    Rule("tg.posts", "tg.posts", "tg",
         "SELECT COUNT(*) FROM posts",
         "SELECT COUNT(*) FROM content WHERE platform='telegram'"),
    Rule("tg.scores", "tg.scores", "tg",
         "SELECT COUNT(*) FROM scores",
         "SELECT COUNT(*) FROM score s JOIN content c ON c.id=s.content_id WHERE c.platform='telegram'"),
    Rule("tg.channel_baselines", "tg.channel_baselines", "tg",
         "SELECT COUNT(*) FROM channel_baselines",
         "SELECT COUNT(*) FROM source_baseline sb JOIN source s ON s.id=sb.source_id "
         "WHERE s.platform='telegram'"),
    Rule("tg.runs", "tg.runs", "tg",
         "SELECT COUNT(*) FROM runs",
         "SELECT COUNT(*) FROM run WHERE platform='telegram'"),
    Rule("tg.run_log", "tg.run_log", "tg",
         "SELECT COUNT(*) FROM run_log",
         "SELECT COUNT(*) FROM run_log WHERE platform='telegram'",
         note="D-24: принадлежность явной колонкой platform"),
    Rule("tg.classified", "tg.classified", "tg",
         "SELECT COUNT(*) FROM classified",
         "SELECT COUNT(*) FROM classification cl JOIN content c ON c.id=cl.content_id "
         "WHERE c.platform='telegram'"),
    Rule("tg.stories", "tg.stories", "tg",
         "SELECT COUNT(*) FROM stories",
         "SELECT COUNT(*) FROM story WHERE platform='telegram'"),
    Rule("tg.story_members", "tg.story_members", "tg",
         "SELECT COUNT(*) FROM story_members",
         "SELECT COUNT(*) FROM story_member sm JOIN story s ON s.id=sm.story_id "
         "WHERE s.platform='telegram'"),
    Rule("tg.metrics_daily", "tg.metrics_daily", "tg",
         "SELECT COUNT(*) FROM metrics_daily",
         "SELECT COUNT(*) FROM metrics_daily WHERE platform='telegram'"),
    Rule("tg.account_state", "tg.account_state", "tg",
         "SELECT COUNT(*) FROM account_state",
         "SELECT COUNT(*) FROM transport_account_state WHERE platform='telegram'"),
)

# Объяснимые расхождения: id правила → причина. По умолчанию пусто.
KNOWN_DIFFS: dict[str, str] = {}


@dataclass
class Result:
    rule: Rule
    before: int | None
    after: int | None
    reason: str = ""

    @property
    def diff(self) -> int | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before

    @property
    def explained(self) -> bool:
        """Расхождение объяснено (ноль или запись в known_diffs)."""
        return self.diff == 0 or bool(self.reason)


def run_parity(
    target_path: str,
    *,
    os_path: str | None = None,
    x_path: str | None = None,
    tg_path: str | None = None,
    known_diffs: dict[str, str] | None = None,
) -> list[Result]:
    """Посчитать все правила. Возвращает список результатов."""
    paths = {"os": os_path, "x": x_path, "tg": tg_path}
    known = dict(KNOWN_DIFFS)
    if known_diffs:
        known.update(known_diffs)

    core = db.connect(target_path)
    legacy_conns: dict[str, object] = {}
    try:
        for key, path in paths.items():
            if path:
                legacy_conns[key] = legacy.open_legacy(path)
        results: list[Result] = []
        for rule in RULES:
            if rule.legacy_db not in legacy_conns:
                continue
            lconn = legacy_conns[rule.legacy_db]
            if not legacy.has_table(lconn, _legacy_base_table(rule)):
                results.append(Result(rule, None, None, "legacy-таблица отсутствует"))
                continue
            before = lconn.execute(rule.legacy_sql).fetchone()[0]
            after = core.execute(rule.core_sql).fetchone()[0]
            reason = ""
            if before != after:
                reason = known.get(rule.id, "")
            results.append(Result(rule, int(before), int(after), reason))
        return results
    finally:
        for conn in legacy_conns.values():
            conn.close()
        core.close()


def _legacy_base_table(rule: Rule) -> str:
    """Первое имя таблицы из таблицы-описания правила (для проверки наличия)."""
    return rule.legacy_table.split("+", 1)[0].split(".", 1)[1]


def format_table(results: list[Result]) -> str:
    lines = []
    lines.append(f"{'правило':<40} {'было':>10} {'стало':>10} {'дельта':>10}  статус")
    lines.append("-" * 90)
    unexplained = 0
    for res in results:
        before = "—" if res.before is None else str(res.before)
        after = "—" if res.after is None else str(res.after)
        diff = "—" if res.diff is None else f"{res.diff:+d}"
        if res.before is None and res.after is None:
            status = f"SKIP ({res.reason})"
        elif res.diff == 0:
            status = "OK"
        elif res.reason:
            status = "OK (known_diff)"
        else:
            status = "РАСХОЖДЕНИЕ"
            unexplained += 1
        lines.append(f"{res.rule.id:<40} {before:>10} {after:>10} {diff:>10}  {status}")

    lines.append("-" * 90)
    if unexplained == 0:
        lines.append("Итог: расхождений нет.")
    else:
        lines.append(f"Итог: необъяснённых расхождений — {unexplained}.")
        lines.append("")
        lines.append("Причины расхождений:")
        for res in results:
            if res.diff not in (0, None) and not res.reason:
                lines.append(
                    f"  * {res.rule.id}: legacy={res.before}, core={res.after} "
                    f"(дельта {res.diff:+d})"
                    + (f" — {res.rule.note}" if res.rule.note else "")
                )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuber parity", description="Сверка legacy ↔ ядро")
    parser.add_argument("--target", required=True)
    parser.add_argument("--os", dest="os_path")
    parser.add_argument("--x", dest="x_path")
    parser.add_argument("--tg", dest="tg_path")
    parser.add_argument("--known-diffs", help="JSON-файл: {id_правила: причина}")
    parser.add_argument("--json", action="store_true", help="вывести результат в JSON")
    args = parser.parse_args(argv)

    known = {}
    if args.known_diffs:
        with open(args.known_diffs, encoding="utf-8") as fh:
            known = json.load(fh)

    results = run_parity(
        args.target,
        os_path=args.os_path,
        x_path=args.x_path,
        tg_path=args.tg_path,
        known_diffs=known,
    )

    if args.json:
        payload = [
            {"id": r.rule.id, "before": r.before, "after": r.after, "diff": r.diff}
            for r in results
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_table(results))

    unexplained = sum(
        1 for r in results if r.diff not in (0, None) and not r.reason
    )
    return 0 if unexplained == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
