#!/usr/bin/env python3
"""Приёмка ТЗ-8 (задачи 1–3) на КОПИИ рабочей БД.

ЖЁСТКИЕ ПРАВИЛА:
  * рабочая БД открывается ТОЛЬКО на чтение (`file:...?mode=ro`);
  * копия делается через sqlite3 backup API (во временном каталоге); все
    прогоны идут на копии: env `TUBER_X_DB=<копия>`;
  * отчёт приёмки пишется ТОЛЬКО во временный каталог через
    `TUBER_X_REPORT_DIR`; рабочий `reports/` не трогается;
  * снимок списка и хэшей файлов `reports/` и `docs/` берётся ДО и ПОСЛЕ:
    изменёнными допустимы только `docs/REPORT-8.md` и
    `docs/acceptance-log-8.txt`;
  * секреты не печатаются;
  * в конце печатаются статусы рабочей БД ДО и ПОСЛЕ и обязательная строка
    «рабочая БД не изменена: ДА/НЕТ».

Запуск: python3 tools/acceptance_tz8.py
Вывод:  docs/acceptance-log-8.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import config  # noqa: E402
from tuber.platforms.x import report as report_mod  # noqa: E402

DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

_out = []
_results = []

# Изменение этих файлов допустимо (их пишет сама приёмка/отчёт ТЗ-8).
ALLOWED_CHANGED = {"docs/REPORT-8.md", "docs/acceptance-log-8.txt"}

STATUS_RE = re.compile(r"/status/(\d+)")
BLOCK_RE = re.compile(r"^(\d)\. ")


def p(line=""):
    print(line)
    _out.append(str(line))


def hr(title):
    p("")
    p("=" * 78)
    p(title)
    p("=" * 78)


def check(num, what, ok, data):
    _results.append({"num": num, "what": what, "ok": bool(ok), "data": data})
    p(f"[{num}] {'OK  ' if ok else 'FAIL'} {what}")
    p(f"      данные: {data}")


# --------------------------------------------------------------- копия/статусы
def prod_snapshot(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    snap = {"accounts": {}, "posts": None, "classified": {}, "stories": None,
            "scores": None, "report_texts": None, "version": None}
    for r in con.execute("SELECT tier, status, COUNT(*) n FROM accounts"
                         " GROUP BY tier, status ORDER BY tier, status"):
        snap["accounts"][f"{r['tier']}/{r['status']}"] = r["n"]
    snap["posts"] = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    for r in con.execute("SELECT status, COUNT(*) n FROM classified"
                         " GROUP BY status ORDER BY status"):
        snap["classified"][r["status"]] = r["n"]
    for r in con.execute("SELECT is_ai, COUNT(*) n FROM classified"
                         " GROUP BY is_ai ORDER BY is_ai"):
        snap["classified"][f"is_ai={r['is_ai']}"] = r["n"]
    snap["stories"] = con.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    snap["scores"] = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    snap["report_texts"] = con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0]
    snap["version"] = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    return snap


def short(snap):
    acc = ", ".join(f"{k}={v}" for k, v in snap["accounts"].items())
    cls = ", ".join(f"{k}={v}" for k, v in snap["classified"].items())
    return (f"user_version={snap['version']} posts={snap['posts']} "
            f"classified[{cls}] stories={snap['stories']} scores={snap['scores']} "
            f"report_texts={snap['report_texts']} | {acc}")


def backup_via_api(src_path, dst_path):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def prod_fingerprint(path):
    snap = prod_snapshot(path)
    return (snap["posts"], tuple(sorted(snap["classified"].items())),
            tuple(sorted(snap["accounts"].items())), snap["stories"],
            snap["scores"], snap["report_texts"])


def dir_snapshot(*dirs):
    """Хэши файлов в каталогах: {относительный путь: sha256}.

    Пустой отпечаток каталога и список файлов тоже важны, поэтому возвращаем и
    карту, и отсортированный список имён.
    """
    out = {}
    for d in dirs:
        for base, _sub, files in os.walk(d):
            for name in files:
                path = os.path.join(base, name)
                rel = os.path.relpath(path, ROOT)
                try:
                    with open(path, "rb") as fh:
                        out[rel] = hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    out[rel] = "unreadable"
    return out


def load_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_file = "/root/.hermes/.env"
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def run_cli(args, env, timeout=1800):
    cmd = [sys.executable, "-m", "tuber x"] + args
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------- метрики задачи 1
def story_numbers(con):
    """(сюжетов всего, целиком из не-ИИ, постов в сюжетах, доля не-ИИ).

    Пост «про ИИ» — только приговор `is_ai=1`. Пост с `is_ai=0` или без
    приговора считается не-ИИ.
    """
    total = con.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    per_story = con.execute(
        """SELECT s.id, SUM(CASE WHEN c.is_ai=1 THEN 1 ELSE 0 END) n_ai
           FROM stories s JOIN story_posts sp ON sp.story_id=s.id
           LEFT JOIN posts p ON p.tweet_id=sp.tweet_id
           LEFT JOIN classified c ON c.text_hash=p.text_hash
           GROUP BY s.id""").fetchall()
    all_non_ai = sum(1 for r in per_story if (r["n_ai"] or 0) == 0)
    posts = con.execute(
        """SELECT COUNT(*) tot, SUM(CASE WHEN c.is_ai=1 THEN 1 ELSE 0 END) ai
           FROM story_posts sp LEFT JOIN posts p ON p.tweet_id=sp.tweet_id
           LEFT JOIN classified c ON c.text_hash=p.text_hash""").fetchone()
    tot = posts["tot"] or 0
    ai = posts["ai"] or 0
    share = ((tot - ai) / tot) if tot else 0.0
    return total, all_non_ai, tot, share


def parse_blocks(text):
    blocks, cur = {}, None
    for line in text.splitlines():
        m = BLOCK_RE.match(line)
        if m:
            cur = m.group(1)
            blocks[cur] = []
        elif cur is not None:
            blocks[cur].append(line)
    return {k: "\n".join(v) for k, v in blocks.items()}


def status_ids(block_text):
    return sorted(set(STATUS_RE.findall(block_text or "")))


def non_ai_ids(con, ids):
    bad = []
    for tid in ids:
        row = con.execute(
            "SELECT c.is_ai FROM posts p LEFT JOIN classified c"
            " ON c.text_hash=p.text_hash WHERE p.tweet_id=?", (tid,)).fetchone()
        if row is None or row["is_ai"] != 1:
            bad.append(tid)
    return bad


# --------------------------------------------------------------------- главное
def main():
    started = datetime.now(timezone.utc)
    prod = os.path.realpath(config.DB_PATH)
    if not os.path.exists(config.DB_PATH):
        p(f"ОШИБКА: рабочая БД не найдена: {config.DB_PATH}")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="tuber_x_acc8_")
    copy_path = os.path.join(tmpdir, "tuber_x_copy.db")
    report_dir = os.path.join(tmpdir, "reports")

    hr("ПРИЁМКА ТЗ-8 (задачи 1–3)")
    p(f"начало:            {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"рабочая БД (RO):   {prod}")

    # ---- снимок рабочих каталогов ДО прогона (задача 2)
    reports_dir = os.path.join(ROOT, "reports")
    docs_dir = os.path.join(ROOT, "docs")
    snap_before = dir_snapshot(reports_dir, docs_dir)
    p(f"снимок reports/ + docs/: файлов {len(snap_before)}")
    for rel in sorted(snap_before):
        p(f"   {snap_before[rel][:16]}  {rel}")

    work_before = prod_snapshot(prod)
    p(f"статусы рабочей БД ДО:  {short(work_before)}")

    backup_via_api(prod, copy_path)
    if os.path.realpath(copy_path) == prod:
        p("ОШИБКА: копия совпала с рабочей БД — приёмка отменена")
        return 3
    p(f"копия (backup API): {copy_path}")
    check(1, "копия создана через sqlite3 backup API и не совпадает с рабочей БД",
          os.path.exists(copy_path) and os.path.realpath(copy_path) != prod,
          f"копия={os.path.basename(copy_path)}")

    env = dict(os.environ)
    env["TUBER_X_DB"] = copy_path
    env["TUBER_X_REPORT_DIR"] = report_dir
    env.setdefault("PYTHONPATH", ROOT)
    key = load_key()
    if key:
        env["DEEPSEEK_API_KEY"] = key

    con = sqlite3.connect(copy_path)
    con.row_factory = sqlite3.Row

    # ---- числа ДО (состояние копии как есть)
    hr("ЗАДАЧА 1. Числа ДО (состояние рабочей выдачи как есть)")
    total_b, allnon_b, posts_b, share_b = story_numbers(con)
    p(f"сюжетов всего={total_b}; целиком из не-ИИ={allnon_b}; "
      f"постов в сюжетах={posts_b}; доля не-ИИ={share_b:.1%}")
    check(2, "числа ДО получены (сюжеты содержат не-ИИ посты)",
          total_b > 0 and allnon_b > 0,
          f"сюжетов={total_b}, целиком из не-ИИ={allnon_b}")

    # ---- полная цепочка на копии
    hr("ЗАДАЧА 1. Пересборка сюжетов и оценок на копии (фильтр is_ai=1)")
    rc_st, out_st = run_cli(["stories", "--limit", "200"], env)
    rc_sc, out_sc = run_cli(["scores", "--limit", "500"], env)
    p("--- stories ---")
    for line in out_st.strip().splitlines()[:3]:
        p("   " + line)
    p("--- scores (хвост) ---")
    for line in out_sc.strip().splitlines()[-3:]:
        p("   " + line)
    check(3, "stories+scores отработали на копии без ошибки",
          rc_st == 0 and rc_sc == 0, f"rc stories={rc_st}, rc scores={rc_sc}")

    # ---- числа ПОСЛЕ
    con.close()
    con = sqlite3.connect(copy_path)
    con.row_factory = sqlite3.Row
    total_a, allnon_a, posts_a, share_a = story_numbers(con)
    hr("ЗАДАЧА 1. Числа ПОСЛЕ (после фильтра is_ai)")
    p(f"сюжетов всего={total_a}; целиком из не-ИИ={allnon_a}; "
      f"постов в сюжетах={posts_a}; доля не-ИИ={share_a:.1%}")
    check(4, "после фильтра нет сюжетов целиком из не-ИИ",
          allnon_a == 0, f"целиком из не-ИИ={allnon_a} (было {allnon_b})")
    check(5, "доля не-ИИ постов в сюжетах равна 0",
          abs(share_a) < 1e-9, f"доля={share_a:.4%} (было {share_b:.1%})")

    # ---- отчёт на копии (во временный каталог)
    hr("ЗАДАЧА 2. Отчёт на копии пишется во временный каталог")
    n_rt_before = con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0]
    cand = report_mod.translation_candidates(con, date=DATE)
    con.close()
    rc_rep, out_rep = run_cli(["report", "--date", DATE], env)
    report_path = os.path.join(report_dir, DATE + ".md")
    has_file = os.path.exists(report_path)
    text = open(report_path, encoding="utf-8").read() if has_file else ""
    headers = ["1. Главное за сутки", "2. По темам", "3. Деньги и запуски",
               "4. Новинки и инструменты", "5. Русскоязычный срез",
               "6. Тёмные лошадки и первые авторы", "7. Служебный блок"]
    missing = [h for h in headers if h not in text]
    inside_tmp = os.path.realpath(report_path).startswith(os.path.realpath(tmpdir))
    check(6, "файл отчёта создан во временном каталоге (reports/ не тронут)",
          has_file and inside_tmp,
          f"{report_path}; размер={len(text)}б; rc={rc_rep}; "
          f"внутри временного каталога={inside_tmp}")
    check(7, "в отчёте присутствуют все 7 блоков", not missing,
          f"нет блоков: {missing or 'нет'}")

    # ---- блоки 1,3,4,6 без постов is_ai=0
    hr("ЗАДАЧА 1. Блоки 1, 3, 4, 6: обход строк отчёта, постов is_ai=0 нет")
    con = sqlite3.connect(copy_path)
    con.row_factory = sqlite3.Row
    blocks = parse_blocks(text)
    all_clean, details = True, []
    for b in ("1", "3", "4", "6"):
        ids = status_ids(blocks.get(b, ""))
        bad = non_ai_ids(con, ids)
        all_clean = all_clean and not bad
        details.append(f"блок {b}: постов={len(ids)}, не-ИИ={bad or 'нет'}")
    for d in details:
        p("   " + d)
    check(8, "в блоках 1,3,4,6 нет ни одного поста с is_ai=0",
          all_clean, "; ".join(details))

    # ---- счётчик отсева в служебном блоке
    m = re.search(r"Отброшено как не про ИИ:\s*(\d+)", blocks.get("7", ""))
    dropped = int(m.group(1)) if m else None
    check(9, "служебный блок показывает число отброшенных не-ИИ постов",
          dropped is not None,
          f"«Отброшено как не про ИИ: {dropped}»")

    # ---- задача 3: честный итог про кэш переводов
    hr("ЗАДАЧА 3. Кэш переводов: кандидаты против прироста строк")
    n_rt_after = con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0]
    con.close()
    tr_status, tr_msg = report_mod.translation_verdict(cand, n_rt_after - n_rt_before)
    p(f"кандидатов на перевод={cand}; строк report_texts {n_rt_before}->{n_rt_after}"
      f" (+{n_rt_after - n_rt_before})")
    p(f"вердикт={tr_status}: {tr_msg}; ключ модели {'есть' if key else 'НЕТ'}")
    check(10, "итог по кэшу переводов честный (ок/нечего/провал)",
          tr_status in ("ok", "nothing"),
          f"вердикт={tr_status}: {tr_msg}")

    # ---- приёмка не тронула рабочие каталоги
    hr("ЗАДАЧА 2. Снимок reports/ и docs/ ДО/ПОСЛЕ, сравнение хэшей")
    snap_after = dir_snapshot(reports_dir, docs_dir)
    p(f"снимок ПОСЛЕ: файлов {len(snap_after)}")
    for rel in sorted(snap_after):
        p(f"   {snap_after[rel][:16]}  {rel}")
    names = sorted(set(snap_before) | set(snap_after))
    changed = [n for n in names
               if snap_before.get(n) != snap_after.get(n)]
    unexpected = [n for n in changed if n not in ALLOWED_CHANGED]
    p(f"изменённых файлов: {len(changed)}")
    for n in changed:
        p(f"   {n}: {str(snap_before.get(n))[:16]} -> {str(snap_after.get(n))[:16]}"
          f"  {'(допустимо)' if n in ALLOWED_CHANGED else '(НЕДОПУСТИМО)'}")
    check(11, "рабочие каталоги reports/ и docs/ не изменены (кроме допустимых)",
          not unexpected, f"недопустимых изменений: {unexpected or 'нет'}")

    # ---- тесты
    hr("ТЕСТЫ")
    proc_t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                            env=env, capture_output=True, text=True)
    tail = (proc_t.stdout or "").strip().splitlines()[-1] if proc_t.stdout else ""
    check(12, "полный набор тестов зелёный", proc_t.returncode == 0,
          f"pytest rc={proc_t.returncode}; {tail}")

    # --------------------------------------------------------- статусы ПОСЛЕ
    hr("СТАТУСЫ РАБОЧЕЙ БД ДО/ПОСЛЕ")
    work_after = prod_snapshot(prod)
    p(f"статусы рабочей БД ДО:    {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    same = (work_before["accounts"] == work_after["accounts"]
            and work_before["posts"] == work_after["posts"]
            and work_before["classified"] == work_after["classified"]
            and work_before["stories"] == work_after["stories"]
            and work_before["scores"] == work_after["scores"]
            and work_before["report_texts"] == work_after["report_texts"])
    check(13, "рабочая БД не изменена приёмкой (снимки ДО/ПОСЛЕ совпали)", same,
          f"posts {work_before['posts']}->{work_after['posts']}; "
          f"stories {work_before['stories']}->{work_after['stories']}")

    hr("ЧИСЛА ЗАДАЧИ 1 (ДО -> ПОСЛЕ)")
    p(f"сюжетов всего:            {total_b} -> {total_a}")
    p(f"из них целиком из не-ИИ:  {allnon_b} -> {allnon_a} (обязано быть 0)")
    p(f"постов в сюжетах:         {posts_b} -> {posts_a}")
    p(f"доля не-ИИ в сюжетах:     {share_b:.1%} -> {share_a:.1%} (обязано быть 0)")
    p(f"отброшено как не про ИИ:  {dropped}")

    hr("СВОДКА")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    hr("ОБЯЗАТЕЛЬНЫЕ СТРОКИ ПРИЁМКИ")
    p(f"рабочая БД: {prod}")
    p(f"статусы рабочей БД ДО:    {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    p("рабочая БД не изменена: " + ("ДА" if same else "НЕТ"))

    out_path = os.path.join(ROOT, "docs", "acceptance-log-8.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
