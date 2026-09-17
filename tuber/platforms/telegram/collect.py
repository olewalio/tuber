#!/usr/bin/env python3
"""Tuber-Telegram: сборщик постов из публичных Telegram-каналов реестра.

Режимы:
  web      — чтение https://t.me/s/<handle> с пагинацией ?before=<id> (без аккаунта);
  mtproto  — Telethon, инкрементальное чтение подписанных каналов (read_mode='mtproto');
  resolve  — резолв кандидатов через MTProto (get_entity + GetFullChannelRequest).

Предохранители квоты: дедлайн прогона, backoff на 403/429, flood_until в БД,
не более 20 резолвов в сутки, sleep между вызовами MTProto.

stdout — ровно одна JSON-строка с итогом. Остальной вывод — в run_log и logs/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from . import config as _config
from . import store as db

#: Пути и параметры — из общего конфига платформы (ТЗ-4). Имена оставлены
#: прежними: их читает остальной код файла и перенесённые тесты.
ROOT = _config.ROOT
DB_PATH = _config.DB_PATH
LOG_DIR = _config.LOG_DIR
SESSION_PATH = "/root/.hermes/swarm/sessions/tg_collector"
ENV_PATH = "/root/.hermes/.env"
ACCOUNT_NAME = _config.ACCOUNT_NAME

MSK = timezone(timedelta(hours=3))
HTTP_TIMEOUT = _config.HTTP_TIMEOUT
WEB_MAX_PAGES = _config.WEB_MAX_PAGES
RESOLVE_MAX_PER_RUN = _config.RESOLVE_MAX_PER_RUN
RESOLVE_MAX_PER_DAY = _config.RESOLVE_MAX_PER_DAY

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
# Схема БД больше не создаётся этим модулем: её владелец — ядро
# (``tuber.core.schema``), а legacy-форму таблиц отдаёт адаптер
# :mod:`tuber.platforms.telegram.store`. Прежний ``SCHEMA_SQL`` удалён — иначе
# прогон создал бы в единой базе настоящие таблицы ``channels``/``posts`` и
# заслонил бы ядро.

# ----------------------------------------------------------------------------
# Время
# ----------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def sql_now() -> str:
    return utcnow().strftime("%Y-%m-%d %H:%M:%S")


def to_msk(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S МСК")
    except Exception:
        return iso


def today_utc() -> str:
    return utcnow().strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# Мини-DOM на stdlib (без внешних парсеров)
# ----------------------------------------------------------------------------
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent

    def get(self, name, default=None):
        return self.attrs.get(name, default)

    @property
    def cls(self) -> str:
        return self.attrs.get("class", "") or ""

    def has_class(self, needle: str) -> bool:
        return needle in self.cls.split()


class DomParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def _append(self, node):
        self.stack[-1].children.append(node)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.stack[-1])
        self._append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.stack[-1])
        self._append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    p = DomParser()
    p.feed(text)
    p.close()
    return p.root


def walk(node: Node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from walk(child)


def find_all(node: Node, *, has_class=None, tag=None, attr=None):
    out = []
    for n in walk(node):
        if tag is not None and n.tag != tag:
            continue
        if has_class is not None and not n.has_class(has_class):
            continue
        if attr is not None and attr not in n.attrs:
            continue
        out.append(n)
    return out


def find_first(node: Node, *, has_class=None, tag=None, attr=None):
    for n in walk(node):
        if tag is not None and n.tag != tag:
            continue
        if has_class is not None and not n.has_class(has_class):
            continue
        if attr is not None and attr not in n.attrs:
            continue
        return n
    return None


def _collect_text(node: Node, out: list):
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag == "br":
            out.append("\n")
        elif child.tag == "a":
            url = child.get("href")
            inner = _render_inline(child)
            if url:
                out.append(f"{inner} ({url})" if inner.strip() else url)
            else:
                out.append(inner)
        else:
            _collect_text(child, out)


def _render_inline(node: Node) -> str:
    out = []
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag == "br":
            out.append("\n")
        else:
            out.append(_render_inline(child))
    return "".join(out)


def render_text(node: Node) -> str:
    """HTML → чистый текст; ссылки как `текст (url)`."""
    out = []
    _collect_text(node, out)
    txt = "".join(out)
    txt = txt.replace("\u00a0", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r" *\n *", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


# ----------------------------------------------------------------------------
# Числа и извлечения
# ----------------------------------------------------------------------------
_SUFFIX = {
    "k": 1_000, "m": 1_000_000, "b": 1_000_000_000,
    "тыс": 1_000, "млн": 1_000_000, "млрд": 1_000_000_000,
}


def parse_count(raw: str):
    if raw is None:
        return None
    s = raw.strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    if not s:
        return None
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-zА-Яа-я]*)", s)
    if not m:
        m2 = re.search(r"([0-9]+)", s)
        return int(m2.group(1)) if m2 else None
    num = float(m.group(1))
    suf = m.group(2).lower()
    if suf in _SUFFIX:
        num *= _SUFFIX[suf]
    return int(round(num))


URL_RE = re.compile(r"https?://[^\s<>\"']+")
HASHTAG_RE = re.compile(r"(?:^|[\s(])#([A-Za-z0-9_]{2,})")
MENTION_RE = re.compile(r"(?:^|[\s(])@([A-Za-z0-9_]{3,})")

INTERNAL_HOSTS = {"t.me", "telegram.me", "telegram.org", "telegram.dog", "telesco.pe"}


# TODO(debt-D-37): реклама ключевыми словами — нужен семантический классификатор — см. TECH-DEBT.md
def is_ad_text(text: str) -> bool:
    if not text:
        return False
    head = text[:200]
    if "erid" in head.lower():
        return True
    if "реклам" in head.lower():
        return True
    if "промокод" in head.lower():
        return True
    return False


def extract_links(text: str):
    if not text:
        return []
    seen = []
    for m in URL_RE.findall(text):
        u = m.rstrip(").,;]")
        if u not in seen:
            seen.append(u)
    return seen


def extract_hashtags(text: str):
    return sorted(set(HASHTAG_RE.findall(text or "")))


def extract_mentions(text: str):
    return sorted(set(MENTION_RE.findall(text or "")))


# ----------------------------------------------------------------------------
# Разбор страницы t.me/s
# ----------------------------------------------------------------------------
MEDIA_CLASSES = [
    ("tgme_widget_message_photo_wrap", "photo"),
    ("tgme_widget_message_video_player", "video"),
    ("tgme_widget_message_video_wrap", "video"),
    ("tgme_widget_message_roundvideo", "roundvideo"),
    ("tgme_widget_message_voice", "voice"),
    ("tgme_widget_message_audio", "audio"),
    ("tgme_widget_message_document_wrap", "document"),
    ("tgme_widget_message_document", "document"),
    ("tgme_widget_message_sticker", "sticker"),
    ("tgme_widget_message_gif", "gif"),
    ("tgme_widget_message_poll", "poll"),
]


def _media_kind(msg: Node):
    for cls, kind in MEDIA_CLASSES:
        if find_first(msg, has_class=cls) is not None:
            return kind
    return None


# Порядок классов-кандидатов на счётчик форвардов в разметке t.me/s.
# ВАЖНО (замер 16.09.2026): публичное веб-превью t.me/s в норме счётчик форвардов
# НЕ отдаёт — в разметке есть только `tgme_widget_message_views` (просмотры) и
# `tgme_widget_message_forwarded_from[_name]` (признак, что публикация — пересылка,
# причём имя источника может начинаться с числа: «Forwarded from 42 секунды»).
# Наивный парс «любого класса со словом forward» даёт ложные форварды (число из
# имени канала-источника) — так был получен ложный счётчик 42. Поэтому классы
# `*forwarded_from*` отсекаются по ПОДСТРОКЕ токена, а не по равенству строки.
# Хелпер остаётся: если Telegram вернёт счётчик (в любом классе с forward, но не
# forwarded_from), он разберётся автоматически. См. docs/TECH-DEBT.md (D-01).


# TODO(debt-D-34): форварды недоступны из t.me/s — нужен MTProto-бэкфилл — см. TECH-DEBT.md
def _is_forward_count_node(node: Node) -> bool:
    for tok in node.cls.split():
        if "forward" in tok and "forwarded_from" not in tok:
            return True
    return False


def extract_forwards(msg: Node):
    """Число форвардов из разметки конкретного сообщения, либо None.

    Узел считается счётчиком, если в его классах есть токен со словом `forward`,
    кроме семейства `forwarded_from` (это имя источника пересылки, не счётчик).
    """
    for n in walk(msg):
        if _is_forward_count_node(n):
            val = parse_count(_render_inline(n))
            if val is not None:
                return val
    return None


def parse_message(msg: Node, handle: str):
    post_attr = msg.get("data-post", "")
    if "/" not in post_attr:
        return None
    try:
        message_id = int(post_attr.rsplit("/", 1)[1])
    except ValueError:
        return None

    # дата
    date_iso = None
    t = find_first(msg, tag="time", attr="datetime")
    if t is not None:
        raw = t.get("datetime")
        try:
            date_iso = iso_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except Exception:
            date_iso = None

    # текст
    text_node = find_first(msg, has_class="js-message_text")
    if text_node is None:
        text_node = find_first(msg, has_class="tgme_widget_message_text")
    text = render_text(text_node) if text_node is not None else ""

    # просмотры
    views = None
    vnode = find_first(msg, has_class="tgme_widget_message_views")
    if vnode is not None:
        views = parse_count(_render_inline(vnode))

    # реакции (сумма по всем реакциям)
    reactions = None
    rnodes = find_all(msg, has_class="tgme_reaction")
    if rnodes:
        total = 0
        for r in rnodes:
            n = parse_count(_render_inline(r))
            if n:
                total += n
        reactions = total

    # форварды: счётчик в веб-превью t.me/s сейчас не публикуется (см. TECH-DEBT.md D-01),
    # поэтому обычно None; хелпер разберёт счётчик, если Telegram вернёт его в разметку.
    forwards = extract_forwards(msg)

    media_kind = _media_kind(msg)
    has_media = media_kind is not None

    # пересылка
    fwd_from = None
    is_forward = 0
    ff = find_first(msg, has_class="tgme_widget_message_forwarded_from")
    if ff is not None:
        name_node = find_first(ff, has_class="tgme_widget_message_forwarded_from_name")
        url = name_node.get("href") if name_node is not None else None
        name = render_text(name_node) if name_node is not None else render_text(ff)
        fwd_from = json.dumps({"name": name, "url": url}, ensure_ascii=False)
        is_forward = 1

    links = extract_links(text)
    ext_links = [u for u in links if urlsplit(u).netloc.lower() not in INTERNAL_HOSTS]

    return {
        "message_id": message_id,
        "date_utc": date_iso,
        "text": text,
        "text_hash": hashlib.sha1(text.encode("utf-8")).hexdigest() if text else None,
        "views": views,
        "forwards": forwards,
        "reactions": reactions,
        "media_kind": media_kind,
        "links": json.dumps(ext_links, ensure_ascii=False),
        "hashtags": json.dumps(extract_hashtags(text), ensure_ascii=False),
        "mentions": json.dumps(extract_mentions(text), ensure_ascii=False),
        "fwd_from": fwd_from,
        "is_forward": is_forward,
        "has_own_media": 1 if (has_media and not is_forward) else 0,
        "is_ad": 1 if is_ad_text(text) else 0,
        "has_media": has_media,
    }


def parse_page(html_text: str, handle: str):
    """Вернуть список постов, разобранных со страницы t.me/s."""
    root = parse_html(html_text)
    posts = []
    for n in walk(root):
        if "data-post" not in n.attrs:
            continue
        if "tgme_widget_message" not in n.cls:
            continue
        p = parse_message(n, handle)
        if p is not None:
            posts.append(p)
    posts.sort(key=lambda p: p["message_id"])
    return posts


def page_is_unreadable(html_text: str) -> bool:
    if "js-widget_message" in html_text or "tgme_widget_message_wrap" in html_text:
        return False
    return True


# ----------------------------------------------------------------------------
# .env
# ----------------------------------------------------------------------------
def load_env(path=ENV_PATH) -> dict:
    env = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


# ----------------------------------------------------------------------------
# Сеть
# ----------------------------------------------------------------------------
def make_client():
    import httpx

    return httpx.Client(
        headers={"User-Agent": DEFAULT_UA, "Accept-Language": "ru,en;q=0.9"},
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    )


def http_get(client, url):
    """Возвращает (status_code, text) или (None, error_str)."""
    r = client.get(url)
    return r.status_code, r.text


# ----------------------------------------------------------------------------
# Коллектор
# ----------------------------------------------------------------------------
class Collector:
    def __init__(self, db_path=DB_PATH, mode="all", handle=None, limit_channels=None,
                 deadline=900, dry_run=False, resolve_n=None, mtproto=False,
                 client=None, sleep_scale=1.0):
        self.db_path = db_path
        self.mode = mode
        self.handle = handle
        self.limit_channels = limit_channels
        self.deadline = deadline
        self.dry_run = dry_run
        self.resolve_n = resolve_n
        self.mtproto_flag = mtproto
        self.sleep_scale = sleep_scale
        self.client = client
        self.con = None
        self.run_id = None
        self.start_monotonic = time.monotonic()
        self.summary = {
            "mode": mode,
            "channels_ok": 0,
            "channels_fail": 0,
            "posts_new": 0,
            "posts_upd": 0,
            "errors": 0,
            "duration_sec": 0.0,
            "flood_until": None,
        }
        self._log_path = os.path.join(LOG_DIR, f"collect-{today_utc()}.log")

    # -- инфраструктура -----------------------------------------------------
    def connect(self):
        # Единая база ядра + слой совместимости с legacy-формой таблиц (ТЗ-4).
        self.con = db.connect(self.db_path)
        return self.con

    def log(self, level, handle, msg):
        line = f"{sql_now()} [{level}] {handle or '-'}: {msg}"
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if self.con is not None and self.run_id is not None and not self.dry_run:
            try:
                self.con.execute(
                    "INSERT INTO run_log(run_id, level, handle, msg) VALUES (?,?,?,?)",
                    (self.run_id, level, handle, msg),
                )
                self.con.commit()
            except sqlite3.Error:
                pass

    def deadline_exceeded(self):
        return (time.monotonic() - self.start_monotonic) >= self.deadline

    def _sleep(self, lo, hi):
        time.sleep(random.uniform(lo, hi) * self.sleep_scale)

    # -- выбор каналов ------------------------------------------------------
    def select_channels(self, for_mtproto=False):
        where = ["status IN ('active','candidate')"]
        params = []
        if for_mtproto:
            where = ["status = 'active'", "read_mode = 'mtproto'"]
        else:
            where.append("(read_mode IS NULL OR read_mode != 'unreadable')")
        if self.handle:
            where = ["handle = ?"]
            params = [self.handle]
        sql = f"SELECT * FROM channels WHERE {' AND '.join(where)}"
        order = (
            " ORDER BY CASE WHEN checked_at IS NULL OR substr(checked_at,1,10) < date('now') "
            "THEN 0 ELSE 1 END, COALESCE(subs,0) DESC, checked_at ASC"
        )
        sql += order
        rows = self.con.execute(sql, params).fetchall()
        if self.limit_channels:
            rows = rows[: self.limit_channels]
        return rows

    # -- web ---------------------------------------------------------------
    def _get_with_backoff(self, url, handle):
        attempts = 0
        while attempts < 3:
            attempts += 1
            try:
                code, text = http_get(self.client, url)
            except Exception as exc:  # noqa: BLE001
                self.log("error", handle, f"request error ({type(exc).__name__}): {exc}")
                self.summary["errors"] += 1
                if attempts >= 3:
                    return None, None
                self._sleep(1.0, 2.0)
                continue
            if code in (403, 429):
                self.log("warn", handle, f"HTTP {code} on {url}, backoff 30s (attempt {attempts}/3)")
                if attempts >= 3:
                    return None, None
                time.sleep(30)
                continue
            if code is None or code >= 500:
                self.log("warn", handle, f"HTTP {code} on {url}")
                if attempts >= 3:
                    return None, None
                self._sleep(1.0, 2.0)
                continue
            return code, text
        return None, None

    def _store_posts(self, channel_id, posts):
        new = upd = 0
        con = self.con
        for p in posts:
            if not p["text"] and not p["has_media"]:
                continue
            exists = con.execute(
                "SELECT views_checked_at FROM posts WHERE channel_id=? AND message_id=?",
                (channel_id, p["message_id"]),
            ).fetchone()
            if self.dry_run:
                if exists is None:
                    new += 1
                else:
                    upd += 1
                continue
            date_utc = p["date_utc"] or sql_now().replace(" ", "T") + "+00:00"
            if exists is None:
                con.execute(
                    """INSERT INTO posts
                       (channel_id, message_id, date_utc, text, text_hash, views, forwards,
                        reactions, media_kind, links, hashtags, mentions, fwd_from,
                        is_forward, has_own_media, is_ad, first_seen_at, views_checked_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END)""",
                    (
                        channel_id, p["message_id"], date_utc, p["text"] or None, p["text_hash"],
                        p["views"], p["forwards"], p["reactions"], p["media_kind"],
                        p["links"], p["hashtags"], p["mentions"], p["fwd_from"],
                        p["is_forward"], p["has_own_media"], p["is_ad"], sql_now(),
                        p["views"] if p["views"] is not None else None,
                    ),
                )
                new += 1
            else:
                # просмотры обновляются не чаще 1 раза в сутки
                con.execute(
                    """UPDATE posts SET
                         views = CASE WHEN views_checked_at IS NULL
                                       OR views_checked_at < datetime('now','-1 day')
                                      THEN COALESCE(?, views) ELSE views END,
                         views_checked_at = CASE WHEN views_checked_at IS NULL
                                       OR views_checked_at < datetime('now','-1 day')
                                      THEN datetime('now') ELSE views_checked_at END,
                         forwards = COALESCE(?, forwards),
                         reactions = COALESCE(?, reactions),
                         media_kind = COALESCE(media_kind, ?),
                         text = CASE WHEN (text IS NULL OR text='') AND ? IS NOT NULL AND ? != ''
                                     THEN ? ELSE text END,
                         text_hash = CASE WHEN (text IS NULL OR text='') AND ? IS NOT NULL AND ? != ''
                                     THEN ? ELSE text_hash END
                       WHERE channel_id=? AND message_id=?""",
                    (
                        p["views"], p["forwards"], p["reactions"], p["media_kind"],
                        p["text"], p["text"], p["text"], p["text"], p["text"], p["text_hash"],
                        channel_id, p["message_id"],
                    ),
                )
                upd += 1
        if not self.dry_run:
            con.commit()
        return new, upd

    def collect_channel_web(self, ch):
        handle = ch["handle"]
        cid = ch["id"]
        row = self.con.execute(
            "SELECT MIN(message_id) AS mn FROM posts WHERE channel_id=?", (cid,)
        ).fetchone()
        min_id = row["mn"] if row else None

        posts = []
        pages = 0
        url = f"https://t.me/s/{handle}"
        first_html = None
        while True:
            code, text = self._get_with_backoff(url, handle)
            if text is None:
                self.summary["channels_fail"] += 1
                self.log("error", handle, "channel failed: no response after retries")
                if not self.dry_run:
                    self.con.execute("UPDATE channels SET checked_at=? WHERE id=?", (sql_now(), cid))
                    self.con.commit()
                return
            if first_html is None:
                first_html = text
            parsed = parse_page(text, handle)
            if not parsed:
                break
            posts.extend(parsed)
            pages += 1
            oldest = min(p["message_id"] for p in parsed)

            if min_id is None:
                # первый контакт с каналом: одной страницы достаточно для инкремента
                break
            if oldest <= min_id:
                break
            if pages >= WEB_MAX_PAGES:
                break
            if self.deadline_exceeded():
                break
            url = f"https://t.me/s/{handle}?before={oldest}"
            self._sleep(0.5, 1.0)

        if not posts and first_html is not None and page_is_unreadable(first_html):
            if not self.dry_run:
                self.con.execute(
                    "UPDATE channels SET read_mode='unreadable', status='private', "
                    "checked_at=?, notes=COALESCE(notes,'')||' | unreadable page' WHERE id=?",
                    (sql_now(), cid),
                )
                self.con.commit()
            self.log("info", handle, "unreadable/empty page -> read_mode=unreadable, status=private")
            self.summary["channels_fail"] += 1
            return

        new, upd = self._store_posts(cid, posts)
        self.summary["posts_new"] += new
        self.summary["posts_upd"] += upd
        self.summary["channels_ok"] += 1
        if not self.dry_run:
            self.con.execute("UPDATE channels SET checked_at=? WHERE id=?", (sql_now(), cid))
            self.con.commit()
        self.log("info", handle, f"ok pages={pages} new={new} upd={upd}")

    def run_web(self):
        if self.client is None:
            self.client = make_client()
        rows = self.select_channels(for_mtproto=False)
        for idx, ch in enumerate(rows):
            if self.deadline_exceeded():
                self.log("info", None, "deadline reached, stopping web scan")
                break
            try:
                self.collect_channel_web(ch)
            except Exception as exc:  # noqa: BLE001
                self.summary["channels_fail"] += 1
                self.summary["errors"] += 1
                self.log("error", ch["handle"], f"exception: {type(exc).__name__}: {exc}")
            if idx != len(rows) - 1 and not self.deadline_exceeded():
                self._sleep(0.5, 1.0)

    # -- MTProto -----------------------------------------------------------
    def _tg_client(self, api_id, api_hash):
        from telethon import TelegramClient

        return TelegramClient(SESSION_PATH, int(api_id), api_hash)

    def _set_flood(self, cid, seconds):
        until = iso_utc(utcnow() + timedelta(seconds=seconds))
        if self.dry_run:
            return until
        self.con.execute(
            """INSERT INTO account_state(name, flood_until, resolves_today, day, updated_at)
               VALUES (?,?,0,?,datetime('now'))""",
            (ACCOUNT_NAME, until, today_utc()),
        )
        if cid is not None:
            self.con.execute("UPDATE channels SET flood_until=? WHERE id=?", (until, cid))
        self.con.commit()
        self.summary["flood_until"] = until
        return until

    def run_mtproto(self):
        from asyncio import get_event_loop

        env = load_env()
        api_id = env.get("TG_API_ID")
        api_hash = env.get("TG_API_HASH")
        if not api_id or not api_hash:
            self.log("error", None, "mtproto skipped: TG_API_ID/TG_API_HASH not set")
            return
        rows = self.select_channels(for_mtproto=True)
        if not rows:
            self.log("info", None, "mtproto: no active channels with read_mode=mtproto")
            return
        loop = get_event_loop()
        loop.run_until_complete(self._run_mtproto_async(rows, api_id, api_hash))

    async def _run_mtproto_async(self, rows, api_id, api_hash):
        from telethon.errors import FloodWaitError

        client = self._tg_client(api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            self.log("error", None, "mtproto: session not authorized")
            await client.disconnect()
            return
        try:
            for ch in rows:
                if self.deadline_exceeded():
                    break
                handle = ch["handle"]
                cid = ch["id"]
                maxrow = self.con.execute(
                    "SELECT COALESCE(MAX(message_id),0) AS mx FROM posts WHERE channel_id=?", (cid,)
                ).fetchone()
                min_id = maxrow["mx"] if maxrow else 0
                posts = []
                try:
                    entity = await client.get_entity(handle)
                    async for m in client.iter_messages(entity, min_id=min_id, reverse=True, limit=200):
                        posts.append(self._mt_message(m))
                        # sleep 1s между вызовами (лимит ~10 запросов/30с)
                        time.sleep(1.0 * self.sleep_scale)
                except FloodWaitError as e:
                    self._set_flood(cid, e.seconds)
                    self.log("error", handle, f"FLOOD_WAIT {e.seconds}s — стоп аккаунта до спада")
                    break
                except Exception as exc:  # noqa: BLE001
                    self.summary["channels_fail"] += 1
                    self.summary["errors"] += 1
                    self.log("error", handle, f"mtproto error: {type(exc).__name__}: {exc}")
                    continue
                new, upd = self._store_posts(cid, posts)
                self.summary["posts_new"] += new
                self.summary["posts_upd"] += upd
                self.summary["channels_ok"] += 1
                if not self.dry_run:
                    self.con.execute("UPDATE channels SET checked_at=? WHERE id=?", (sql_now(), cid))
                    self.con.commit()
                self.log("info", handle, f"mtproto ok new={new} upd={upd}")
        finally:
            await client.disconnect()

    def _mt_message(self, m):
        text = m.message or ""
        media_kind = None
        if getattr(m, "photo", None):
            media_kind = "photo"
        elif getattr(m, "video", None):
            media_kind = "video"
        elif getattr(m, "document", None):
            media_kind = "document"
        fwd_from = None
        is_forward = 0
        if getattr(m, "fwd_from", None):
            is_forward = 1
            fwd_from = json.dumps({"peer_id": str(getattr(m.fwd_from, "from_id", None))}, ensure_ascii=False)
        return {
            "message_id": m.id,
            "date_utc": iso_utc(m.date.replace(tzinfo=timezone.utc)) if m.date else None,
            "text": text,
            "text_hash": hashlib.sha1(text.encode("utf-8")).hexdigest() if text else None,
            "views": getattr(m, "views", None),
            "forwards": getattr(m, "forwards", None),
            "reactions": None,
            "media_kind": media_kind,
            "links": json.dumps([u for u in extract_links(text) if urlsplit(u).netloc.lower() not in INTERNAL_HOSTS], ensure_ascii=False),
            "hashtags": json.dumps(extract_hashtags(text), ensure_ascii=False),
            "mentions": json.dumps(extract_mentions(text), ensure_ascii=False),
            "fwd_from": fwd_from,
            "is_forward": is_forward,
            "has_own_media": 1 if media_kind and not is_forward else 0,
            "is_ad": 1 if is_ad_text(text) else 0,
            "has_media": media_kind is not None,
        }

    # -- resolve -----------------------------------------------------------
    def ensure_account_state(self):
        self.con.execute(
            "INSERT OR IGNORE INTO account_state(name, flood_until, resolves_today, day, updated_at) "
            "VALUES (?,NULL,0,?,datetime('now'))",
            (ACCOUNT_NAME, today_utc()),
        )
        self.con.commit()

    def resolve_gate(self, requested):
        """(allowed, effective, reason) с учётом flood_until и дневного лимита."""
        self.ensure_account_state()
        row = self.con.execute(
            "SELECT flood_until, resolves_today, day FROM account_state WHERE name=?", (ACCOUNT_NAME,)
        ).fetchone()
        flood_until = row["flood_until"] if row else None
        if flood_until:
            try:
                if datetime.fromisoformat(flood_until) > utcnow():
                    self.summary["flood_until"] = flood_until
                    return False, 0, f"skip: flood until {to_msk(flood_until)}"
            except Exception:
                pass
        used = row["resolves_today"] if row and row["day"] == today_utc() else 0
        remaining = RESOLVE_MAX_PER_DAY - used
        effective = min(requested if requested is not None else RESOLVE_MAX_PER_RUN,
                        RESOLVE_MAX_PER_RUN, remaining)
        if effective <= 0:
            return False, 0, f"skip: daily resolve limit reached ({used}/{RESOLVE_MAX_PER_DAY})"
        return True, effective, f"resolve budget {effective} (used {used}/{RESOLVE_MAX_PER_DAY})"

    def select_resolve_targets(self, effective):
        sql = ("SELECT * FROM channels WHERE status='candidate' AND tg_id IS NULL "
               "ORDER BY COALESCE(subs,0) DESC, checked_at ASC LIMIT ?")
        return self.con.execute(sql, (effective,)).fetchall()

    def run_resolve(self):
        requested = self.resolve_n if self.resolve_n is not None else RESOLVE_MAX_PER_RUN
        allowed, effective, reason = self.resolve_gate(requested)
        self.log("info", None, reason)
        if not allowed:
            return
        targets = self.select_resolve_targets(effective)
        if not targets:
            self.log("info", None, "resolve: no candidate channels with tg_id IS NULL")
            return
        env = load_env()
        api_id = env.get("TG_API_ID")
        api_hash = env.get("TG_API_HASH")
        if not api_id or not api_hash:
            self.log("error", None, "resolve skipped: TG_API_ID/TG_API_HASH not set")
            return
        from asyncio import get_event_loop

        loop = get_event_loop()
        loop.run_until_complete(self._run_resolve_async(targets, api_id, api_hash))

    async def _run_resolve_async(self, targets, api_id, api_hash):
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.channels import GetFullChannelRequest

        client = self._tg_client(api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            self.log("error", None, "resolve: session not authorized")
            await client.disconnect()
            return
        done = 0
        try:
            for ch in targets:
                if self.deadline_exceeded():
                    break
                handle = ch["handle"]
                try:
                    entity = await client.get_entity(handle)
                    full = await client(GetFullChannelRequest(entity))
                    about = getattr(full.full_chat, "about", None)
                    subs = getattr(full.full_chat, "participants_count", None)
                    title = getattr(entity, "title", None)
                    if not self.dry_run:
                        self.con.execute(
                            "UPDATE channels SET tg_id=?, subs=?, subs_at=?, title=?, "
                            "notes=COALESCE(?, notes) WHERE id=?",
                            (entity.id, subs, today_utc(), title, about, ch["id"]),
                        )
                    self.summary["channels_ok"] += 1
                    done += 1
                    self.log("info", handle, f"resolved tg_id={entity.id} subs={subs}")
                except FloodWaitError as e:
                    self._set_flood(ch["id"], e.seconds)
                    self.log("error", handle, f"FLOOD_WAIT {e.seconds}s — стоп резолва")
                    break
                except Exception as exc:  # noqa: BLE001
                    if not self.dry_run:
                        self.con.execute(
                            "UPDATE channels SET status='dead', notes=? WHERE id=?",
                            (f"resolve failed: {type(exc).__name__}: {exc}", ch["id"]),
                        )
                    self.summary["channels_fail"] += 1
                    self.log("error", handle, f"resolve failed: {type(exc).__name__}: {exc}")
                self._sleep(3.0, 5.0)
            if not self.dry_run and done:
                self.ensure_account_state()
                cur = self.con.execute(
                    "SELECT resolves_today, day FROM account_state WHERE name=?", (ACCOUNT_NAME,)
                ).fetchone()
                used = cur["resolves_today"] if cur and cur["day"] == today_utc() else 0
                self.con.execute(
                    "UPDATE account_state SET resolves_today=?, day=?, updated_at=datetime('now') WHERE name=?",
                    (used + done, today_utc(), ACCOUNT_NAME),
                )
                self.con.commit()
        finally:
            await client.disconnect()

    # -- оркестрация -------------------------------------------------------
    def modes(self):
        m = self.mode
        if m == "all":
            out = ["web", "mtproto"]
            if self.resolve_n is not None:
                out.append("resolve")
        elif m in ("web", "mtproto", "resolve"):
            out = [m]
        else:
            out = ["web"]
        if self.mtproto_flag and "mtproto" not in out:
            out.append("mtproto")
        if m == "resolve" and self.resolve_n is None:
            self.resolve_n = RESOLVE_MAX_PER_RUN
        return out

    def execute(self):
        self.connect()
        note_modes = self.modes()
        if not self.dry_run:
            self.con.execute(
                "INSERT INTO runs(started_at, mode) VALUES (?,?)", (sql_now(), self.mode)
            )
            # ``lastrowid`` у представления всегда 0: настоящий id возвращает
            # триггер адаптера (store.last_insert_id).
            self.run_id = db.last_insert_id(self.con, "run")
            self.con.commit()
        self.log("info", None, f"run start mode={self.mode} modes={note_modes} deadline={self.deadline}s")
        try:
            for m in note_modes:
                if self.deadline_exceeded() and m != "web":
                    self.log("info", None, f"deadline reached before mode={m}")
                    break
                if m == "web":
                    self.run_web()
                elif m == "mtproto":
                    self.run_mtproto()
                elif m == "resolve":
                    self.run_resolve()
        except Exception as exc:  # noqa: BLE001
            self.summary["errors"] += 1
            self.log("error", None, f"fatal: {type(exc).__name__}: {exc}")
        finally:
            self.summary["duration_sec"] = round(time.monotonic() - self.start_monotonic, 2)
            self.summary["flood_until"] = self.summary["flood_until"] or self._current_flood()
            if not self.dry_run:
                self.con.execute(
                    """UPDATE runs SET finished_at=?, channels_ok=?, channels_fail=?,
                       posts_new=?, posts_upd=?, errors=?, note=? WHERE id=?""",
                    (
                        sql_now(), self.summary["channels_ok"], self.summary["channels_fail"],
                        self.summary["posts_new"], self.summary["posts_upd"], self.summary["errors"],
                        json.dumps({"modes": note_modes, "msk": to_msk(utcnow().isoformat())}, ensure_ascii=False),
                        self.run_id,
                    ),
                )
                self.con.commit()
            self.log("info", None, f"run done {json.dumps(self.summary, ensure_ascii=False)}")
        return self.summary

    def _current_flood(self):
        try:
            row = self.con.execute(
                "SELECT flood_until FROM account_state WHERE name=?", (ACCOUNT_NAME,)
            ).fetchone()
            if row and row["flood_until"]:
                return row["flood_until"]
        except sqlite3.Error:
            pass
        return None


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Tuber-Telegram collector")
    ap.add_argument("--mode", choices=["web", "mtproto", "resolve", "all"], default="all")
    ap.add_argument("--handle", default=None, help="отладка одного канала")
    ap.add_argument("--limit-channels", type=int, default=None)
    ap.add_argument("--deadline", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mtproto", action="store_true", help="включить режим mtproto")
    ap.add_argument("--resolve", nargs="?", type=int, const=RESOLVE_MAX_PER_RUN, default=None,
                    help="включить режим resolve, максимум N (по умолчанию 20)")
    ap.add_argument("--db", default=DB_PATH)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    c = Collector(
        db_path=args.db,
        mode=args.mode,
        handle=args.handle,
        limit_channels=args.limit_channels,
        deadline=args.deadline,
        dry_run=args.dry_run,
        resolve_n=args.resolve,
        mtproto=args.mtproto,
    )
    try:
        result = c.execute()
    except Exception as exc:  # noqa: BLE001
        result = {
            "mode": args.mode, "channels_ok": 0, "channels_fail": 0,
            "posts_new": 0, "posts_upd": 0, "errors": 1,
            "duration_sec": 0.0, "flood_until": None,
        }
        sys.stderr.write(f"fatal: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
