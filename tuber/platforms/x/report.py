"""Ежедневная выдача (ТЗ-3 Р4).

Отчёт печатается в stdout и сохраняется в `reports/YYYY-MM-DD.md`. Семь
пронумерованных блоков, простые строки, без эмодзи. Если в блоке нет данных —
пишется «нет данных за сутки»; блок молча не пропускается и содержимое не
выдумывается. Ни один содержательный пункт не выводится без автора, ссылки и
проверенной даты (`published_at_utc`).

Перевод текстов делается ТОЛЬКО здесь (Р1.6) и кэшируется в `report_texts`,
чтобы один и тот же текст не переводить дважды. Сеть не вызывается напрямую:
используется канал DeepSeek (`channels.py`).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import ai_filter, config, store as db, scores, stories
from tuber.core import urls

NO_DATA = "нет данных за сутки"
# Задача 6 ТЗ виральности: успешный код возврата не должен маскировать пустую
# работу. Если за сутки нет постов ИЛИ нет строк значимости за сутки — в
# служебный блок идёт явная строка, а CLI возвращает ненулевой код, чтобы это
# заметили обёртка и сторож (проверка `report_data` в health.py).
EMPTY_DAY_WARNING = ("ВНИМАНИЕ: за сутки нет данных — проверь сбор "
                     "(постов за сутки 0 и/или строк значимости 0)")

MONEY_RE = re.compile(
    r"(?:\$|€|₽)\s?([0-9]+(?:[.,][0-9]+)?)\s?"
    r"(billion|million|thousand|млрд|млн|тыс|B|M|K|bn|mm)\b", re.I)
UNIT_RU = {"billion": "млрд", "b": "млрд", "bn": "млрд",
           "million": "млн", "m": "млн", "mm": "млн",
           "thousand": "тыс", "k": "тыс",
           "млрд": "млрд", "млн": "млн", "тыс": "тыс"}


# --------------------------------------------------------------------- даты
def day_bounds(date_str=None, now=None):
    now = now or datetime.now(timezone.utc)
    if date_str:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return d, d + timedelta(days=1)


def _iso(dt):
    return db.iso(dt)


def _link(url, handle, tweet_id):
    """Ссылка материала: значение из базы, иначе синтез (D-45).

    Правило выдачи: непустой ``content.url`` (если он есть) печатается как
    есть — база главнее; пустой — синтезируется единым хелпером
    :func:`tuber.core.urls.content_url`. Выдуманных ссылок нет: не хватает
    компонентов — ``None``.
    """
    return urls.material_url(url, "x", tweet_id, handle)


def _post_handle(post):
    return db.post_author(post)


def _has_date(post):
    return bool(db.parse_iso(post["published_at_utc"]))


def _fmt_dt(value):
    dt = db.parse_iso(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "?"


def _amount_ru(text):
    if not text:
        return None
    m = MONEY_RE.search(text)
    if not m:
        return None
    num = m.group(1).replace(",", ".")
    unit = UNIT_RU.get(m.group(2).lower(), m.group(2))
    return f"{num} {unit}"


# --------------------------------------------------------------------- перевод
class ReportTranslator:
    """Перевод/сжатие текста в русский с кэшем и лимитом на отчёт (Р1.6)."""

    SYSTEM = (
        "Переведи пост на русский язык одной короткой фразой (до 30 слов). "
        "Сохрани имена, названия моделей и чисел. Верни строго JSON "
        "{\"ru\": \"...\"} без пояснений."
    )

    def __init__(self, con, client=None, budget=None):
        self.con = con
        self.budget = int(budget if budget is not None else config.REPORT_TRANSLATE_BUDGET)
        self.used = 0
        self.calls = 0
        self.client = client
        if self.client is None and config.REPORT_TRANSLATE:
            self.client = self._maybe_default()

    @staticmethod
    def _maybe_default():
        try:
            from . import channels
            b = channels.DeepSeekBroker()
            return b if b.available() else None
        except Exception:
            return None

    def translate(self, text_hash, text):
        if not text_hash or not text:
            return None
        row = self.con.execute("SELECT ru FROM report_texts WHERE text_hash=?",
                               (text_hash,)).fetchone()
        if row and row["ru"]:
            return row["ru"]
        if not self.client or self.used >= self.budget:
            return None
        try:
            res = self.client.classify(self.SYSTEM, text[:config.CLASSIFY_MAX_TEXT_CHARS],
                                       max_tokens=400)
            self.calls += 1
            data = json.loads(res["content"])
            ru = (data.get("ru") or "").strip()
        except Exception:
            return None
        if not ru:
            return None
        self.used += 1
        self.con.execute("INSERT OR REPLACE INTO report_texts (text_hash, ru, model,"
                         " created_at, src) VALUES (?,?,?,?,'model')",
                         (text_hash, ru, getattr(self.client, "model", None),
                          db.utcnow_iso()))
        self.con.commit()
        return ru

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass


# --------------------------------------------------------------------- блоки
# Тайбрейк «первого поста сюжета» (D-27). В legacy порядок держался на
# физическом порядке строк: запрос ``WHERE sp.story_id=?`` читал
# ``story_posts`` по первичному ключу ``(story_id, tweet_id)``
# (``sqlite_autoindex_story_posts_1``), поэтому при равном
# ``published_at_utc`` побеждал пост с лексикографически меньшим
# ``tweet_id``. В ядре ``story_member`` упорядочен по ``(story_id, content_id)``
# — это порядок ВСТАВКИ в ``content``, а не семантический, и тайбрейк от
# порядка миграции зависеть не должен. Тот же вторичный ключ уже используется
# в кластеризации (``stories.assign_roles``/``cluster_posts``: ``(published_at_utc,
# str(tweet_id))``), поэтому здесь он объявлен явно: результат тот же, что у
# legacy, но воспроизводим без опоры на план запроса.
_FIRST_POST_ORDER = " ORDER BY p.published_at_utc ASC, p.tweet_id ASC LIMIT 1"


def _primary_post(con, story):
    row = con.execute(
        "SELECT p.*, a.handle AS acc_handle FROM story_posts sp"
        " JOIN posts p ON p.tweet_id=sp.tweet_id"
        " LEFT JOIN accounts a ON a.id=p.account_id"
        " WHERE sp.story_id=?" + _FIRST_POST_ORDER,
        (story["id"],)).fetchone()
    return row


def _story_claim(con, story_id):
    row = con.execute(
        "SELECT c.topic, c.subtopic, c.claim_type, c.text_hash, p.text, p.lang"
        " FROM story_posts sp JOIN posts p ON p.tweet_id=sp.tweet_id"
        " LEFT JOIN classified c ON c.text_hash=p.text_hash"
        " WHERE sp.story_id=?" + _FIRST_POST_ORDER,
        (story_id,)).fetchone()
    return row


def _story_description(con, story, translator):
    claim = _story_claim(con, story["id"])
    topics = _json(story["topics"]) or []
    template = None
    if claim and claim["topic"]:
        template = f"{claim['topic']}"
        if claim["subtopic"]:
            template += f" — {claim['subtopic']}"
    if claim and claim["text"] and translator is not None:
        ru = translator.translate(claim["text_hash"], claim["text"])
        if ru:
            return ru
    if template:
        return template
    return "Сюжет без тематической рубрики"


def _json(value):
    if not value:
        return None
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _main_rows(con, start, end):
    """Сюжеты главного блока в окне суток (та же выборка, что видит отчёт)."""
    return [s for s in stories.main_stories(con, limit=config.REPORT_MAIN_LIMIT + 5)
            if _in_window(s["published_at"], start, end)][:config.REPORT_MAIN_LIMIT]


def block1_main(con, start, end, translator):
    lines = ["1. Главное за сутки"]
    rows = _main_rows(con, start, end)
    if not rows:
        lines.append(f"   {NO_DATA}")
        return lines
    for i, s in enumerate(rows, 1):
        first = _primary_post(con, s)
        if first is None or not _has_date(first):
            continue
        desc = _story_description(con, s, translator)
        lead = "" if s["lead_time_min"] is None else f", второй автор через {s['lead_time_min']:.0f} мин"
        lines.append(f"   {i}. {desc}")
        lines.append(f"      Первый: @{s['first_mover']}; независимых авторов: {s['xconf']};"
                     f" время: {_fmt_dt(s['published_at'])}")
        lines.append(f"      Ссылка: {_link(first['url'], db.post_author(first), first['tweet_id'])}{lead}")
    if len(lines) == 1:
        lines.append(f"   {NO_DATA}")
    return lines


def translation_candidates(con, *, date=None, now=None):
    """Сколько текстов отчёт реально попытается перевести (ТЗ-8 задача 3).

    Кандидат — текст зачина сюжета главного блока, которого ещё нет в кэше
    `report_texts`. Считается запросом по фактическим данным базы.
    """
    start, end = day_bounds(date, now=now)
    n = 0
    for s in _main_rows(con, start, end):
        first = _primary_post(con, s)
        if first is None or not _has_date(first):
            continue
        claim = _story_claim(con, s["id"])
        if not claim or not claim["text"] or not claim["text_hash"]:
            continue
        row = con.execute("SELECT ru FROM report_texts WHERE text_hash=?",
                          (claim["text_hash"],)).fetchone()
        if row and row["ru"]:
            continue
        n += 1
    return n


def translation_verdict(candidates, rows_added):
    """Честный итог пункта приёмки про кэш переводов (ТЗ-8 задача 3).

    Возвращает (status, message): status один из "ok" / "nothing" / "fail".
    Пустая таблица при нуле кандидатов — это НЕ «кэш наполнен».
    """
    if int(candidates) <= 0:
        return "nothing", "переводить было нечего, кандидатов 0"
    if int(rows_added) <= 0:
        return "fail", (f"кандидатов на перевод {int(candidates)},"
                        f" а новых строк в report_texts 0")
    return "ok", f"переведено {int(rows_added)} из {int(candidates)} кандидатов"


def block2_topics(con, start, end):
    lines = ["2. По темам"]
    seven = start - timedelta(days=7)
    today = {}
    week = {}
    for s in con.execute("SELECT topics, published_at FROM stories"
                         " WHERE published_at >= ? AND published_at < ?",
                         (_iso(seven), _iso(end))):
        topics = _json(s["topics"]) or []
        topic = topics[0] if topics else None
        if not topic:
            continue
        t = db.parse_iso(s["published_at"])
        week[topic] = week.get(topic, 0) + 1
        if start <= t < end:
            today[topic] = today.get(topic, 0) + 1
    total_today = sum(today.values())
    if not today and not week:
        lines.append(f"   {NO_DATA}")
        return lines
    rows = [(k, today.get(k, 0), week.get(k, 0)) for k in set(today) | set(week)]
    rows = [r for r in rows if r[1] or r[2]]
    rows.sort(key=lambda r: (-(r[1] / total_today if total_today else 0), -r[2], r[0]))
    lines.append("   рубрика | сюжетов сегодня | сюжетов за 7 дней | доля сегодняшняя")
    for k, t, w in rows:
        share = (t / total_today) if total_today else 0.0
        lines.append(f"   {k} | {t} | {w} | {share:.0%}")
    return lines


def _dedup_kind(kind, details):
    """ТЗ-5 задача 5: убрать дубль слова рубрики в подписи.

    Было: «Раунд: раунд Sugar», «Запуск: запуск локальные модели». Стало:
    «Раунд: Sugar», «Запуск: локальные модели». Если подпись схлопывается в
    само слово рубрики — пишем «без уточнения».
    """
    d = (details or "").strip()
    if not d:
        return "без уточнения"
    kl = kind.lower()
    dl = d.lower()
    if dl == kl:
        return "без уточнения"
    for sep in (": ", ":", " — ", " - ", " ", ", "):
        if dl.startswith(kl + sep):
            d = d[len(kind):].lstrip(" :—-").strip()
            break
    return d or "без уточнения"


def _money_lines(con, start, end):
    out = []
    rows = con.execute(
        """SELECT DISTINCT s.* FROM stories s
           JOIN story_posts sp ON sp.story_id=s.id
           JOIN posts p ON p.tweet_id=sp.tweet_id
           JOIN classified c ON c.text_hash=p.text_hash
           WHERE c.claim_type IN ('funding','release')
             AND s.published_at >= ? AND s.published_at < ?
           ORDER BY s.xconf DESC""", (_iso(start), _iso(end))).fetchall()
    for s in rows:
        first = _primary_post(con, s)
        if first is None or not _has_date(first):
            continue
        claim = _story_claim(con, s["id"])
        amount = _amount_ru(claim["text"] if claim else None)
        kind = "Раунд" if (claim and claim["claim_type"] == "funding") else "Запуск"
        who = db.post_author(first)
        details = _dedup_kind(kind, claim["subtopic"] if claim else None)
        amt = f", сумма: {amount}" if amount else ""
        out.append(f"   {kind}: {details}{amt}; кто: @{who}; "
                   f"{_fmt_dt(s['published_at'])}; "
                   f"{_link(first['url'], who, first['tweet_id'])}")
    return out


def block3_money(con, start, end):
    lines = ["3. Деньги и запуски"]
    body = _money_lines(con, start, end)
    lines.extend(body or [f"   {NO_DATA}"])
    return lines


def block4_tools(con, start, end):
    lines = ["4. Новинки и инструменты"]
    # ТЗ-5 задача 5: «инфраструктура и железо» (дата-центры, чипы, питание,
    # верификация инфраструктуры) — это НЕ новинки и инструменты разработчика.
    rubrics = ("инструменты разработчика", "вайб-кодинг")
    exclude = {"инфраструктура и железо"}
    out = []
    for s in con.execute("SELECT * FROM stories WHERE published_at >= ? AND published_at < ?"
                         " ORDER BY xconf DESC, published_at DESC",
                         (_iso(start), _iso(end))):
        topics = _json(s["topics"]) or []
        if not (set(topics) & set(rubrics)) or set(topics) & exclude or s["suspect"]:
            continue
        first = _primary_post(con, s)
        if first is None or not _has_date(first):
            continue
        claim = _story_claim(con, s["id"])
        detail = (claim["subtopic"] if claim and claim["subtopic"]
                  else ", ".join(topics))
        who = db.post_author(first)
        out.append(f"   {detail}; рубрика: {', '.join(topics)}; кто: @{who}; "
                   f"{_fmt_dt(s['published_at'])}; {_link(first['url'], who, first['tweet_id'])}")
    lines.extend(out or [f"   {NO_DATA}"])
    return lines


def block5_russian(con, start, end):
    lines = ["5. Русскоязычный срез"]
    # ТЗ-8 задача 1: и в отчёте участвуют только посты с приговором is_ai=1.
    rows = con.execute(
        "SELECT p.tweet_id, p.text, p.published_at_utc, p.text_hash, p.lang, p.url,"
        " a.handle AS acc_handle, p.owner_handle, p.author_handle, c.topic, c.subtopic"
        " FROM posts p LEFT JOIN accounts a ON a.id=p.account_id"
        " LEFT JOIN classified c ON c.text_hash=p.text_hash"
        " " + ai_filter.ai_join("p", "c_ai")
        + " WHERE p.lang='ru' AND p.published_at_utc >= ? AND p.published_at_utc < ?"
        " AND p.deleted_at IS NULL"
        " ORDER BY p.published_at_utc DESC LIMIT ?",
        (_iso(start), _iso(end), config.REPORT_RU_LIMIT)).fetchall()
    out = []
    for r in rows:
        if not _has_date(r):
            continue
        handle = db.post_author(r)
        ru = r["text"]
        topic = f" [{r['topic']}]" if r["topic"] else ""
        out.append(f"   @{handle}{topic}: {_short(ru)}; {_fmt_dt(r['published_at_utc'])};"
                   f" {_link(r['url'], handle, r['tweet_id'])}")
    lines.extend(out or [f"   {NO_DATA}"])
    return lines


def _short(text, limit=220):
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def _latest_story_link(con, handle):
    row = con.execute(
        "SELECT s.first_tweet_id, s.first_mover, s.published_at, p.url"
        " FROM stories s"
        " JOIN story_posts sp ON sp.story_id=s.id"
        " JOIN posts p ON p.tweet_id=sp.tweet_id"
        " WHERE lower(sp.handle)=? ORDER BY s.published_at DESC LIMIT 1",
        (handle.lower(),)).fetchone()
    if not row or not row["first_tweet_id"]:
        return None, None
    return _link(row["url"], row["first_mover"], row["first_tweet_id"]), row["published_at"]


def block6_darks(con, start, end):
    lines = ["6. Тёмные лошадки и первые авторы"]
    dk = scores.darks(con, persist=False)
    if dk:
        lines.append("   Тёмные лошадки (рост уникальных сюжетов, вне топ-20):")
        shown = 0
        for d in dk[:10]:
            url, pub = _latest_story_link(con, d["handle"])
            if url is None:
                continue
            shown += 1
            lines.append(f"   @{d['handle']}: сюжетов за неделю {d['stories_cur']}"
                         f" (было {d['stories_prev']}, рост {d['growth']}); "
                         f"последний сюжет: {url} ({_fmt_dt(pub)})")
        if shown == 0:
            lines.append("   тёмные лошадки: нет данных с подтверждённой ссылкой")
    else:
        lines.append("   тёмные лошадки: " + NO_DATA)
    fm = con.execute("SELECT handle, first_mover_score FROM accounts"
                     " WHERE first_mover_score IS NOT NULL AND first_mover_score > 0"
                     " ORDER BY first_mover_score DESC LIMIT ?",
                     (config.REPORT_FIRST_MOVERS,)).fetchall()
    if fm:
        lines.append("   Первые авторы (first_mover_score, полураспад 30 дней):")
        for r in fm:
            url, pub = _latest_story_link(con, r["handle"])
            if url is None:
                continue
            lines.append(f"   @{r['handle']}: {r['first_mover_score']:.3f}; "
                         f"последний сюжет: {url} ({_fmt_dt(pub)})")
    else:
        lines.append("   первые авторы: " + NO_DATA)
    return lines


def block7_service(con, start, end, day=None):
    day = day or start.strftime("%Y-%m-%d")
    lines = ["7. Служебный блок"]
    total = con.execute("SELECT COUNT(*) FROM posts WHERE published_at_utc >= ?"
                        " AND published_at_utc < ?", (_iso(start), _iso(end))).fetchone()[0]
    lines.append(f"   Постов за сутки: {total}")
    status = data_status(con, start, end)
    lines.append(f"   Строк значимости за сутки: {status['significance']}"
                 f" (всего в scores: {status['significance_total']})")
    if status["empty"]:
        lines.append(f"   {EMPTY_DAY_WARNING}")
    # ТЗ-8 задача 1: показываем размер отсева не-ИИ постов, чтобы клиент видел
    # его числом, а не догадывался, почему выдача короче.
    dropped = ai_filter.dropped_non_ai(con, _iso(start), _iso(end))
    lines.append(f"   Отброшено как не про ИИ: {dropped}")
    dup = con.execute("SELECT COUNT(*) FROM (SELECT text_hash FROM posts"
                      " WHERE published_at_utc >= ? AND published_at_utc < ?"
                      " GROUP BY text_hash HAVING COUNT(*) > 1)",
                      (_iso(start), _iso(end))).fetchone()[0]
    lines.append(f"   Дублей текста среди них: {dup}")
    # ТЗ-5 задача 5: если блок 5 пуст, пустота должна быть объяснена числом
    # русскоязычных постов в базе, а не выглядеть поломкой.
    ru_total = con.execute("SELECT COUNT(*) FROM posts WHERE lang='ru'").fetchone()[0]
    ru_day = con.execute("SELECT COUNT(*) FROM posts WHERE lang='ru'"
                         " AND first_seen_at LIKE ?", (day + "%",)).fetchone()[0]
    lines.append(f"   Русскоязычных постов в базе: {ru_total} (за сутки: {ru_day})")
    row = con.execute("SELECT valid_date_ratio FROM metrics_daily WHERE day=?",
                      (day,)).fetchone()
    if row and row["valid_date_ratio"] is not None:
        lines.append(f"   Доля валидных дат за сутки: {row['valid_date_ratio']:.2%}")
    else:
        valid = con.execute(
            "SELECT COUNT(*) FROM posts WHERE published_at_utc >= ? AND"
            " published_at_utc < ? AND published_at_utc IS NOT NULL"
            " AND published_at_utc != '' AND published_at_utc NOT LIKE '1970%'",
            (_iso(start), _iso(end))).fetchone()[0]
        ratio = (valid / total) if total else None
        lines.append(f"   Доля валидных дат за сутки: "
                     f"{(ratio if ratio is not None else 0):.2%}")
    req = con.execute("SELECT COUNT(*) n, SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok"
                      " FROM requests WHERE ts LIKE ?", (day + "%",)).fetchone()
    fail_rate = ((req["n"] - (req["ok"] or 0)) / req["n"]) if req["n"] else 0.0
    lines.append(f"   Отказы сбора за сутки: {req['n'] - (req['ok'] or 0)} из {req['n']}"
                 f" ({fail_rate:.1%})")
    alive = con.execute("SELECT COUNT(*) FROM instances WHERE healthy=1").fetchone()[0]
    lines.append(f"   Живые инстансы: {alive}")
    spent = con.execute("SELECT COUNT(*) FROM requests WHERE ts LIKE ?",
                        (day + "%",)).fetchone()[0]
    pct = (100.0 * spent / (config.DAILY_REQUEST_CAP * max(1, len(config.INSTANCES))))
    lines.append(f"   Расход квоты Nitter за сутки: {spent} запросов"
                 f" ({pct:.2f}% от потолка)")
    cand = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    since48 = _iso(db.utcnow() - timedelta(hours=48))
    new_active = con.execute("SELECT COUNT(*) FROM accounts WHERE status='active'"
                             " AND COALESCE(verified_at, added_at) >= ?",
                             (since48,)).fetchone()[0]
    lines.append(f"   Кандидаты: {cand}; новых active за 48 ч: {new_active}")
    sus = con.execute("SELECT COUNT(*) FROM stories WHERE suspect=1"
                      " AND published_at >= ? AND published_at < ?",
                      (_iso(start), _iso(end))).fetchone()[0]
    lines.append(f"   Подозрительные сюжеты (suspect) за сутки: {sus}")
    return lines


def _in_window(value, start, end):
    t = db.parse_iso(value)
    return bool(t and start <= t < end)


def data_status(con, start, end):
    """Есть ли за сутки содержательные данные (задача 6 ТЗ виральности).

    Пусто, если за окно 0 постов ИЛИ 0 строк значимости по этим постам. Второе
    ловит сломавшийся шаг `scores`, который иначе выглядел бы как «успешный»
    отчёт с «нет данных за сутки» во всех блоках.
    """
    posts = con.execute(
        "SELECT COUNT(*) FROM posts WHERE published_at_utc >= ? AND published_at_utc < ?",
        (_iso(start), _iso(end))).fetchone()[0]
    sig_day = con.execute(
        "SELECT COUNT(*) FROM scores s JOIN posts p ON p.tweet_id=s.tweet_id"
        " WHERE p.published_at_utc >= ? AND p.published_at_utc < ?",
        (_iso(start), _iso(end))).fetchone()[0]
    sig_total = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    return {"posts": posts, "significance": sig_day, "significance_total": sig_total,
            "empty": bool(posts == 0 or sig_day == 0)}


# --------------------------------------------------------------------- сборка
def build(con, *, date=None, translator=None, now=None):
    start, end = day_bounds(date, now=now)
    if translator is False:
        t = None
    elif translator is None:
        t = ReportTranslator(con)
    else:
        t = translator
    lines = []
    lines.append(f"# Tuber-x, выдача за {start.strftime('%Y-%m-%d')}")
    lines.append("")
    for block in (lambda: block1_main(con, start, end, t),
                  lambda: block2_topics(con, start, end),
                  lambda: block3_money(con, start, end),
                  lambda: block4_tools(con, start, end),
                  lambda: block5_russian(con, start, end),
                  lambda: block6_darks(con, start, end),
                  lambda: block7_service(con, start, end, day=start.strftime("%Y-%m-%d"))):
        lines.extend(block())
        lines.append("")
    if isinstance(t, ReportTranslator):
        t.close()
    return "\n".join(lines).rstrip() + "\n"


def write(text, date=None):
    import os
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    name = (date or datetime.now(timezone.utc).strftime("%Y-%m-%d")) + ".md"
    path = os.path.join(config.REPORT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
