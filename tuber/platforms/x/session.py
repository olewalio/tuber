"""Свой транспорт чтения X на сессии нашего выделенного аккаунта (ТЗ-43A/44).

Первый по приоритету канал платформы ``x``: внутренние GraphQL-операции самого
X (``UserByScreenName``, ``UserTweets``, ``TweetDetail``,
``TweetResultsByRestIds``, ``ExplorePage``) на куках нашего аккаунта. Пул
Nitter-зеркал мёртв (из 12 инстансов работал один и мерцал), а свой транспорт
даёт живые данные и, главное, просмотры, которых Nitter не отдаёт вовсе.

Грабли протокола (проверено вживую 21.09.2026, не догадки):

1. HTML-страницы ``x.com`` принимают ТОЛЬКО куки. Если к валидной сессии
   приложить ``authorization: Bearer`` на навигационный запрос, X отвечает
   ``401`` с пустым телом. Это выглядит как «сессия протухла», хотя сессия жива.
2. Bearer + ``x-csrf-token`` (значение куки ``ct0``) + ``x-twitter-auth-type``
   + ``x-twitter-active-user`` уходят ТОЛЬКО на ``/i/api/graphql``.
3. ``queryId`` берутся из родного бандла X (авторизованная страница →
   ``main.<hash>.js`` → регулярка по ``queryId``/``operationName``). Кэш в файл,
   TTL 24 ч, перекачка при 404. Списков из интернета и из памяти модели нет.
4. Набор ФИЧ обязателен: без него X отдаёт урезанный ответ (нет просмотров и
   нет ответов в треде). Имена ищутся в бандле вокруг
   ``view_counts_everywhere_api_enabled`` и передаются как ``{имя: true}``.

Весь выход в сеть — через :func:`tuber.platforms.x.broker.raw_http_get`
(единственный выход проекта, ТЗ-1 Р7.9); прямого сетевого вызова здесь нет.

Ключи и куки не печатаются в логи никогда: ни целиком, ни частями.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta, timezone

from . import config
from .broker import raw_http_get

# Публичный web-bearer X (как в родном веб-клиенте). Не секрет аккаунта.
BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
          "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
API = "https://x.com/i/api/graphql"
HOME = "https://x.com/"
# Ищем ссылку на бандл ровно так, как сказано в ТЗ (не шире и не уже).
BUNDLE_RE = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web/[a-z0-9.\-]+\.js")
FEATURE_PROBE = "view_counts_everywhere_api_enabled"


class SessionUnavailable(RuntimeError):
    """Файла сессии нет, права шире 600 или файл нечитаем.

    Это НЕ отказ Nitter: сбор обязан продолжить работу на старом канале.
    """


class SessionBlocked(RuntimeError):
    """401/403: сессия недействительна — транспорт помечен blocked."""


class XSessionRateLimited(RuntimeError):
    """Лимит аккаунта или cooldown после 429 — уходим на Nitter."""


class XSessionDailyCap(XSessionRateLimited):
    """Суточный потолок запросов исчерпан (запись в run_log, откат на Nitter).

    Наследует :class:`XSessionRateLimited`: для вызывающих это тот же «надо
    уходить на Nitter», а отдельный тип нужен для понятной диагностики.
    """


class XSessionWindowCap(XSessionRateLimited):
    """Потолок окна запросов исчерпан (ТЗ-44C).

    Это НЕ отказ аккаунта и НЕ 429: предохранитель сработал штатно. Наследует
    :class:`XSessionRateLimited`, чтобы старые вызывающие (фолбэк на Nitter)
    не сломались, но точечные задачи (подписчики) отличают его от настоящего
    отказа и просто откладывают аккаунт до освобождения окна.
    """


class XSessionError(RuntimeError):
    """Прочие отказы транспорта сессии."""


# --------------------------------------------------------------- утилиты времени
_CREATED_RE = None


def parse_created_at(raw):
    """``created_at`` поста X → ISO-строка UTC (``YYYY-MM-DDTHH:MM:SS``).

    Формат X: ``Wed Oct 10 20:19:24 +0000 2018``. Возвращает ``None``, если
    строки нет или она не разобралась.
    """
    if not raw or not isinstance(raw, str):
        return None
    global _CREATED_RE
    if _CREATED_RE is None:
        _CREATED_RE = re.compile(
            r"^[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) "
            r"([+-]\d{4}) (\d{4})$")
    m = _CREATED_RE.match(raw.strip())
    if not m:
        return None
    mon, day, hh, mm, ss, tz, year = m.groups()
    try:
        dt = datetime.strptime(
            f"{year}-{mon}-{day} {hh}:{mm}:{ss} {tz}", "%Y-%b-%d %H:%M:%S %z")
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------ транспорт
class XSessionTransport:
    """Клиент внутреннего GraphQL X на сессии аккаунта (ТЗ-43A/44)."""

    def __init__(self, *, session_file=None, qids_file=None, db_path=None,
                 transport=None, clock=time.monotonic, sleeper=time.sleep,
                 wall_clock=time.time, run_id=None, con=None):
        self.session_file = session_file or config.X_SESSION_FILE
        self.qids_file = qids_file or config.X_QIDS_FILE
        self._transport = transport or self._default_transport
        self._clock = clock
        self._sleeper = sleeper
        self._wall = wall_clock
        self.run_id = run_id
        self._owns_con = con is None
        if con is not None:
            self._con = con
        else:
            from . import store as _db
            self._con = _db.connect(db_path or config.DB_PATH,
                                    check_same_thread=False)
        self.auth_token = None
        self.ct0 = None
        self.cookie = None
        self.qids = {}
        self.features = {}
        self._user_ids = {}
        self._last_call = 0.0
        self._calls = deque()          # времена фактических запросов в окне
        # ТЗ-43C: cooldown и счётчик 429 живут в БД (общие для всех процессов),
        # в памяти — только кэш эха последней записи и время последнего warn.
        self._state = {"cooldown_until": None, "consecutive_429": 0,
                       "blocked": False, "last_429_at": None}
        self._last_budget_warn = float("-inf")
        self.stats = {"ok": 0, "429": 0, "err": 0, "blocked": 0, "cap": 0,
                      "budget_denied": 0}
        self._load_session()
        # Читаем cooldown ДО обращения к сети: конструктор не должен бить в X,
        # если аккаунт ещё в штрафе (ТЗ-43C, дыра 1).
        self._reload_state()
        self._load_qids()

    # ------------------------------------------------------------- сессия
    def _load_session(self):
        path = self.session_file
        if not os.path.exists(path):
            raise SessionUnavailable(
                f"файла сессии X нет: {path} (ожидается {{auth_token, ct0}} с правами 600)")
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as e:
            raise SessionUnavailable(f"файл сессии X недоступен: {e}") from None
        if mode & 0o077:
            raise SessionUnavailable(
                f"права файла сессии X шире 600 ({oct(mode)}): {path}")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            raise SessionUnavailable(f"файл сессии X нечитаем: {type(e).__name__}") from None
        token = (data or {}).get("auth_token")
        ct0 = (data or {}).get("ct0")
        if not token or not ct0:
            raise SessionUnavailable("в файле сессии X нет auth_token/ct0")
        self.auth_token = token
        self.ct0 = ct0
        self.cookie = f"auth_token={token}; ct0={ct0}"

    # --------------------------------------------------------- низкий уровень
    @staticmethod
    def _default_transport(url, headers):
        return raw_http_get(url, config.X_SESSION_TIMEOUT_SEC, headers=headers)

    def _warn(self, msg, level="WARN"):
        """Запись в run_log; секретов в сообщении нет по построению."""
        try:
            from . import store as _db
            _db.log_run(self._con, level, msg, run_id=self.run_id)
            self._con.commit()
        except Exception:
            pass

    def _pace(self):
        """Ограничитель под контролем: окно + минимальный интервал.

        Ожидание идёт через инъецируемый ``sleeper`` (в тестах — виртуальные
        часы), поэтому фактических запросов в окне никогда не больше лимита.
        """
        now = self._clock()
        dt = now - self._last_call
        if dt < config.X_SESSION_MIN_INTERVAL:
            self._sleeper(config.X_SESSION_MIN_INTERVAL - dt)
            now = self._clock()
        window = config.X_SESSION_RATE_WINDOW_SEC
        rate = config.X_SESSION_RATE_MAX
        while True:
            while self._calls and (now - self._calls[0]) >= window:
                self._calls.popleft()
            if len(self._calls) < rate:
                break
            wait = window - (now - self._calls[0])
            self._sleeper(max(0.0, wait))
            now = self._clock()
        self._calls.append(now)
        self._last_call = now

    # ------------------------------------------- состояние из БД (ТЗ-43C)
    def _now(self):
        """Абсолютное «настенное» время UTC (cooldown хранится им, а не монотоникой)."""
        return datetime.fromtimestamp(self._wall(), tz=timezone.utc)

    def _reload_state(self):
        """Перечитать cooldown/счётчик 429 из БД (общие для всех процессов)."""
        try:
            from . import store as _db
            self._state = _db.read_session_state(self._con, config.X_SESSION_HOST)
        except Exception:
            pass
        return self._state

    def _check_available(self):
        """Отказ по cooldown/блокировке; значение ЧИТАЕТСЯ ИЗ БАЗЫ (ТЗ-43C)."""
        st = self._reload_state()
        now = self._now()
        until = st.get("cooldown_until")
        if until is not None and now < until:
            left = int((until - now).total_seconds())
            if st.get("blocked"):
                raise SessionBlocked(
                    f"сессия X помечена blocked (401/403), ещё {left} с")
            raise XSessionRateLimited(f"cooldown сессии X ещё {left} с")

    def _check_budget(self):
        """Бюджет запросов по ЖУРНАЛУ ``transport_request`` (ТЗ-43C).

        Журнал ведут ВСЕ процессы, поэтому бюджет общий по построению. Запрос
        НЕ уходит; поднимается ``XSessionRateLimited`` по-русски, фолбэк — Nitter.
        """
        from . import store as _db
        try:
            b = _db.session_budget(self._con)
        except Exception:  # noqa: BLE001 — нет возможности посчитать = не блокируем
            return
        if b["daily_exhausted"]:
            self.stats["cap"] += 1
            self.stats["budget_denied"] += 1
            self._budget_warn(
                f"сессия X: суточный бюджет исчерпан ({b['daily_count']}/"
                f"{b['daily_cap']} за 24 ч) — откат на Nitter, лимит откроется"
                f" в {b['daily_free_at']}")
            raise XSessionDailyCap(
                f"суточный бюджет сессии X исчерпан: сделано {b['daily_count']} из "
                f"{b['daily_cap']} за 24 ч, лимит откроется в {b['daily_free_at']}"
                f" — откат на Nitter")
        if b["window_exhausted"]:
            self.stats["budget_denied"] += 1
            self._budget_warn(
                f"сессия X: бюджет окна исчерпан ({b['window_count']}/"
                f"{b['window_cap']} за {b['window_sec']} с) — откат на Nitter,"
                f" окно освободится в {b['window_free_at']}")
            raise XSessionWindowCap(
                f"бюджет окна сессии X исчерпан: сделано {b['window_count']} из "
                f"{b['window_cap']} за {b['window_sec']} с, окно освободится в "
                f"{b['window_free_at']} — откат на Nitter")

    def _budget_warn(self, msg):
        """Строка про бюджет в run_log — не чаще одного раза в минуту."""
        now = self._wall()
        if now - self._last_budget_warn >= 60.0:
            self._last_budget_warn = now
            self._warn(msg, level="WARN")

    def _note_success(self):
        """200: сброс счётчика подряд идущих 429 и признака blocked в БД."""
        st = self._reload_state()
        if st.get("consecutive_429") or st.get("blocked"):
            try:
                from . import store as _db
                _db.write_session_state(self._con, consecutive_429=0, blocked=0)
                self._state["consecutive_429"] = 0
                self._state["blocked"] = False
            except Exception:
                pass

    def _http(self, url, *, api=False, cookie=True, timeout=None):
        self._check_available()
        self._check_budget()
        self._pace()
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        if api:
            headers.update({
                "Accept": "*/*",
                "content-type": "application/json",
                "Referer": HOME,
                "authorization": f"Bearer {BEARER}",
                "x-csrf-token": self.ct0,
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "en",
            })
        else:
            headers["Accept"] = "text/html,application/xhtml+xml"
        # Куки — на страницы x.com; authorization — ТОЛЬКО на API (иначе HTML
        # отдаёт 401). На CDN-бандл куки не отправляем вовсе.
        if cookie:
            headers["Cookie"] = self.cookie
        t0 = self._clock()
        status, _hdrs, body = self._transport(url, headers)
        latency_ms = int(max(0.0, self._clock() - t0) * 1000)
        self._log_request(url, status, latency_ms)
        return status, body

    def _log_request(self, url, status, latency_ms):
        try:
            from . import store as _db
            _db.record_transport_request(
                self._con, config.X_SESSION_HOST, "x_session",
                self._safe_url(url), status, 0, latency_ms, self.run_id)
        except Exception:
            pass

    @staticmethod
    def _safe_url(url):
        """URL без переменных и куки (в журнал транспорта уходит без секретов)."""
        return url.split("?", 1)[0][:300]

    def _save_state(self, **fields):
        try:
            from . import store as _db
            _db.update_transport_instance(self._con, config.X_SESSION_HOST, **fields)
        except Exception:
            pass

    # ------------------------------------- идентификаторы операций (ТЗ-43A п.3)
    def refresh_query_ids(self):
        """Перекачать бандл X: queryId всех операций + набор фич."""
        status, html = self._http(HOME, api=False)
        if status != 200:
            raise SessionUnavailable(
                f"x.com отдал {status} на страницу с куками — сессия не принята")
        if 'lang="ru"' not in html and "auth_token" not in html and "main." not in html:
            raise SessionUnavailable("страница x.com отдана как гостевая — куки не приняты")
        bundles = BUNDLE_RE.findall(html)
        main = next((b for b in bundles
                     if os.path.basename(b).startswith("main.")), None)
        if main is None:
            raise SessionUnavailable("в авторизованной странице нет ссылки на бандл X")
        status2, js = self._http(main, api=False, cookie=False)
        if status2 != 200:
            raise SessionUnavailable(f"бандл X не скачался: {status2}")
        found = {}
        for qid, op in re.findall(
                r'queryId:"([A-Za-z0-9_-]{15,})",operationName:"([A-Za-z]+)"', js):
            found[op] = qid
        for op, qid in re.findall(
                r'operationName:"([A-Za-z]+)",queryId:"([A-Za-z0-9_-]{15,})"', js):
            found[op] = qid
        features = {}
        i = js.find(FEATURE_PROBE)
        if i > 0:
            start = js.rfind("[", 0, i)
            if start >= 0:
                depth, end = 0, None
                for j in range(start, min(len(js), start + 60000)):
                    if js[j] == "[":
                        depth += 1
                    elif js[j] == "]":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                block = js[start:end + 1] if end else js[start:start + 20000]
                names = re.findall(r'"([a-z][a-z0-9_]{3,60})"', block)
                features = {n: True for n in dict.fromkeys(names)}
        if not found:
            raise SessionUnavailable("в бандле X не найдено ни одной операции")
        self.qids, self.features = found, features
        self._write_qids_file(found, features, main)
        self._warn(f"сессия X: queryId {len(found)}, фич {len(features)} "
                   f"(бандл {os.path.basename(main)})", level="INFO")
        return len(found)

    def _write_qids_file(self, qids, features, bundle):
        try:
            os.makedirs(os.path.dirname(self.qids_file), exist_ok=True)
            tmp = self.qids_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"fetched_at": int(time.time()), "bundle": bundle,
                           "qids": qids, "features": features}, fh,
                          ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.qids_file)
        except OSError:
            pass

    def _load_qids(self):
        path = self.qids_file
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            self.qids = data.get("qids") or {}
            self.features = data.get("features") or {}
            age = time.time() - (data.get("fetched_at") or 0)
            if (age > config.X_SESSION_QIDS_TTL_SEC or not self.qids
                    or not self.features):
                try:
                    self.refresh_query_ids()
                except SessionUnavailable:
                    if not self.qids:
                        raise
        else:
            self.refresh_query_ids()

    # ---------------------------------------------------------------- запросы
    def _gql(self, op, variables, *, with_features=True, field_toggles=None):
        if op not in self.qids:
            self.refresh_query_ids()
            if op not in self.qids:
                raise XSessionError(f"операция {op} отсутствует в бандле X")
        url = self._gql_url(op, variables, with_features, field_toggles)
        status, body = self._http(url, api=True)
        if status == 404:
            # Операция переехала: перекачиваем бандл и пробуем РОВНО один раз.
            self.refresh_query_ids()
            if op not in self.qids:
                raise XSessionError(f"операция {op} пропала из бандла X после перекачки")
            url = self._gql_url(op, variables, with_features, field_toggles)
            status, body = self._http(url, api=True)
        if status == 429:
            self.stats["429"] += 1
            self._handle_429()
            raise XSessionRateLimited(f"429 на {op}: cooldown сессии X")
        if status in (401, 403):
            self.stats["blocked"] += 1
            self._block(status)
            raise SessionBlocked(f"{status} на {op}: сессия недействительна")
        if status != 200:
            self.stats["err"] += 1
            raise XSessionError(f"{op}: http={status}")
        self.stats["ok"] += 1
        self._note_success()
        try:
            return json.loads(body)
        except ValueError:
            raise XSessionError(f"{op}: ответ не JSON") from None

    def _gql_url(self, op, variables, with_features, field_toggles):
        params = [("variables", json.dumps(variables, separators=(",", ":")))]
        if with_features and self.features:
            params.append(("features", json.dumps(self.features, separators=(",", ":"))))
        if field_toggles:
            params.append(("fieldToggles", json.dumps(field_toggles, separators=(",", ":"))))
        qs = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params)
        return f"{API}/{self.qids[op]}/{op}?{qs}"

    def _handle_429(self):
        """429: cooldown пишется в БД; два подряд за сутки → жёсткий cooldown."""
        st = self._reload_state()
        now = self._now()
        consecutive = int(st.get("consecutive_429") or 0) + 1
        last = st.get("last_429_at")
        if last is not None and (now - last) > timedelta(hours=24):
            consecutive = 1  # старый счётчик не переживает сутки
        hard = consecutive >= 2
        secs = (config.X_SESSION_429_COOLDOWN_HARD_SEC if hard
                else config.X_SESSION_429_COOLDOWN_SEC)
        until = now + timedelta(seconds=secs)
        try:
            from . import store as _db
            _db.write_session_state(
                self._con, cooldown_until=until, consecutive_429=consecutive,
                blocked=0, last_429_at=now, last_error="429 Too Many Requests")
        except Exception:
            pass
        self._state = {"cooldown_until": until, "consecutive_429": consecutive,
                       "blocked": False, "last_429_at": now}
        self._warn(f"сессия X: 429, cooldown {secs} с (подряд {consecutive}"
                   + (", жёсткий)" if hard else ")"))

    def _block(self, status):
        """401/403: блокировка на сутки, cooldown пишется в БД (общий)."""
        now = self._now()
        until = now + timedelta(seconds=config.X_SESSION_403_COOLDOWN_SEC)
        try:
            from . import store as _db
            _db.write_session_state(
                self._con, cooldown_until=until, blocked=True,
                last_error=f"HTTP {status}: сессия X недействительна")
            _db.update_transport_instance(self._con, config.X_SESSION_HOST,
                                          healthy=0, rss_ok=0)
        except Exception:
            pass
        self._state["cooldown_until"] = until
        self._state["blocked"] = True
        self._warn(f"сессия X: HTTP {status} — транспорт помечен blocked")

    # ---------------------------------------------------------------- разборы
    @staticmethod
    def _tweets(node, out=None):
        """Собрать Tweet-объекты из любого таймлайна (entries/items/тред)."""
        if out is None:
            out = []
        if isinstance(node, dict):
            if "tweet_results" in node:
                r = node["tweet_results"].get("result")
                if isinstance(r, dict) and r.get("rest_id"):
                    out.append(r)
            for v in node.values():
                XSessionTransport._tweets(v, out)
        elif isinstance(node, list):
            for v in node:
                XSessionTransport._tweets(v, out)
        return out

    @staticmethod
    def _entities(lg):
        ents = lg.get("entities") or {}
        core = (lg.get("extended_entities") or {}).get("media") \
            or ents.get("media") or []
        return ents, core

    @staticmethod
    def _media_kind(lg):
        _ents, media = XSessionTransport._entities(lg)
        kinds = [m.get("type") for m in media if isinstance(m, dict)]
        if not kinds:
            return None
        if "video" in kinds or "animated_gif" in kinds:
            return "video"
        return "photo"

    @staticmethod
    def _quoted_author(tw):
        q = ((tw.get("quoted_status_result") or {}).get("result") or {})
        core = ((q.get("core") or {}).get("user_results") or {}).get("result") or {}
        return ((core.get("core") or {}).get("screen_name")
                or (core.get("legacy") or {}).get("screen_name"))

    @staticmethod
    def _retweeted_author(tw):
        rt = ((tw.get("legacy") or {}).get("retweeted_status_result") or {}).get("result") or {}
        core = ((rt.get("core") or {}).get("user_results") or {}).get("result") or {}
        return ((core.get("core") or {}).get("screen_name")
                or (core.get("legacy") or {}).get("screen_name"))

    @staticmethod
    def parse_tweet(tw):
        """Tweet-объект GraphQL → плоский словарь с метриками и сущностями."""
        lg = tw.get("legacy") or {}
        u = ((tw.get("core") or {}).get("user_results") or {}).get("result") or {}
        core = u.get("core") or {}
        note = ((tw.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
        text = lg.get("full_text") or note.get("text")
        ents, _media = XSessionTransport._entities(lg)
        links = []
        for url in ents.get("urls") or []:
            val = url.get("expanded_url") or url.get("url")
            if val:
                links.append(val)
        author = core.get("screen_name") or (lg.get("user") or {}).get("screen_name")
        mentions = []
        for m in ents.get("user_mentions") or []:
            name = (m.get("screen_name") or "").lower()
            if name and name != (author or "").lower():
                mentions.append(name)
        hashtags = []
        for h in ents.get("hashtags") or []:
            if h.get("text"):
                hashtags.append(h["text"])
        is_quote = bool(lg.get("is_quote_status"))
        is_rt = bool(lg.get("retweeted_status_id_str"))
        orig = None
        if is_rt:
            orig = XSessionTransport._retweeted_author(tw)
        elif is_quote:
            orig = XSessionTransport._quoted_author(tw)
        created = lg.get("created_at")
        published = parse_created_at(created)
        published_src = "x_session" if published else "unknown"
        return {
            "id": tw.get("rest_id"),
            "author": author,
            "author_id": u.get("rest_id"),
            "author_followers": (u.get("relationship_counts") or {}).get("followers")
                                or (u.get("legacy") or {}).get("followers_count"),
            "created_at": created,
            "published_at_utc": published,
            "published_src": published_src,
            "text": text,
            "lang": lg.get("lang"),
            "likes": lg.get("favorite_count"),
            "replies": lg.get("reply_count"),
            "retweets": lg.get("retweet_count"),
            "quotes": lg.get("quote_count"),
            "views": (tw.get("views") or {}).get("count"),
            "is_retweet": is_rt,
            "is_quote": is_quote,
            "is_reply": bool(lg.get("in_reply_to_status_id_str")),
            "reply_to": lg.get("in_reply_to_status_id_str"),
            "orig_handle": (orig or "").lower() or None,
            "links": list(dict.fromkeys(links)),
            "mentions": list(dict.fromkeys(mentions)),
            "hashtags": list(dict.fromkeys(hashtags)),
            "media_kind": XSessionTransport._media_kind(lg),
        }

    @staticmethod
    def _cursor(d, kind="Bottom"):
        for m in re.finditer(
                r'"cursorType"\s*:\s*"([A-Za-z]+)"\s*,\s*"value"\s*:\s*"([^"]+)"',
                json.dumps(d)):
            if m.group(1) == kind:
                return m.group(2)
        return None

    # --------------------------------------------------------------- операции
    def profile(self, handle):
        handle = str(handle).lstrip("@")
        d = self._gql("UserByScreenName", {"screen_name": handle},
                      with_features=False,
                      field_toggles={"withAuxiliaryUserLabels": True})
        u = ((d.get("data") or {}).get("user") or {}).get("result") or {}
        if not u:
            return None
        core = u.get("core") or {}
        privacy = u.get("privacy") if isinstance(u.get("privacy"), dict) else {}
        return {
            "id": u.get("rest_id"),
            "handle": core.get("screen_name") or handle,
            "name": core.get("name"),
            "created_at": core.get("created_at"),
            "followers": (u.get("relationship_counts") or {}).get("followers"),
            "following": (u.get("relationship_counts") or {}).get("following"),
            "tweets": (u.get("tweet_counts") or {}).get("tweets"),
            "bio": (u.get("profile_bio") or {}).get("description"),
            "verified": (u.get("verification") or {}).get("verified"),
            "protected": (privacy or {}).get("protected"),
        }

    def resolve_user_id(self, handle):
        handle = str(handle).lstrip("@").lower()
        if handle in self._user_ids:
            return self._user_ids[handle]
        prof = self.profile(handle)
        uid = (prof or {}).get("id")
        if uid:
            self._user_ids[handle] = uid
        return uid

    def timeline(self, user_id, count=None, cursor=None):
        """Лента пользователя. Возвращает (список разобранных постов, курсор)."""
        count = min(int(count or config.X_SESSION_TIMELINE_COUNT),
                    config.X_SESSION_TIMELINE_MAX)
        variables = {"userId": str(user_id), "count": count,
                     "includePromotedContent": False, "withVoice": False}
        if cursor:
            variables["cursor"] = cursor
        d = self._gql("UserTweets", variables,
                      field_toggles={"withArticlePlainText": False})
        return [self.parse_tweet(t) for t in self._tweets(d)], self._cursor(d)

    def thread(self, tweet_id, limit=50):
        """Пост + ответы (комментарии) с метриками."""
        d = self._gql("TweetDetail", {
            "focalTweetId": str(tweet_id), "with_rux_injections": False,
            "rankingMode": "Relevance", "includePromotedContent": True,
            "withCommunity": True, "withBirdwatchNotes": True, "withVoice": True,
        }, field_toggles={"withArticlePlainText": False})
        return [self.parse_tweet(t) for t in self._tweets(d)][:limit]

    def tweets_by_ids(self, ids):
        """Батч-гидратация: ``data.tweetResult`` — СПИСОК."""
        ids = [str(i) for i in ids]
        d = self._gql("TweetResultsByRestIds", {
            "tweetIds": ids, "includePromotedContent": False,
            "withCommunity": True, "withVoice": False,
        }, field_toggles={"withArticlePlainText": False})
        raw = (d.get("data") or {}).get("tweetResult") or []
        out = []
        for item in raw:
            r = item.get("result") if isinstance(item, dict) else None
            if isinstance(r, dict) and r.get("rest_id"):
                out.append(self.parse_tweet(r))
        return out

    def trends(self, country="UnitedStates"):
        """ExplorePage: страну X игнорирует (ТЗ-43A), оставляем для полноты."""
        d = self._gql("ExplorePage", {"country": country}, with_features=True)
        txt = json.dumps(d)
        names = re.findall(r'"trend_name"\s*:\s*"([^"]{2,60})"', txt)
        if not names:
            names = re.findall(r'"name"\s*:\s*"([^"]{2,40})"', txt)
        return list(dict.fromkeys(names))

    # ------------------------------------------------------------- контракт поста
    @staticmethod
    def to_post(info, owner_handle=None):
        """Разобранный пост → контракт поста (как ``broker.parse_rss``) + метрики."""
        owner = (owner_handle or info.get("author") or "").lstrip("@").lower() or None
        return {
            "tweet_id": str(info.get("id")) if info.get("id") else None,
            "owner_handle": owner,
            "orig_handle": info.get("orig_handle"),
            "published_at_utc": info.get("published_at_utc"),
            "published_src": info.get("published_src") or "x_session",
            "text": info.get("text"),
            "links": info.get("links") or [],
            "mentions": info.get("mentions") or [],
            "hashtags": info.get("hashtags") or [],
            "is_retweet": 1 if info.get("is_retweet") else 0,
            "is_quote": 1 if info.get("is_quote") else 0,
            "is_reply": 1 if info.get("is_reply") else 0,
            "media_kind": info.get("media_kind"),
            "cursor_next": None,
            # Метрики сессионного транспорта (Nitter их не даёт).
            "likes": info.get("likes"),
            "replies": info.get("replies"),
            "reposts": info.get("retweets"),
            "views": info.get("views"),
            "quotes": info.get("quotes"),
            "author_followers": info.get("author_followers"),
            "author_id": info.get("author_id"),
        }

    def fetch_feed(self, handle, count=None, cursor=None):
        """Лента аккаунта в контракте поста. Первый канал для платформы `x`."""
        uid = self.resolve_user_id(handle)
        if not uid:
            raise XSessionError(f"профиль @{str(handle).lstrip('@')} не найден")
        infos, cursor_next = self.timeline(uid, count=count, cursor=cursor)
        posts = []
        for info in infos:
            post = self.to_post(info, owner_handle=handle)
            if not post["tweet_id"] or not post["published_at_utc"]:
                continue  # без id/даты пост бесполезен для дедупа и инварианта Р2
            post["cursor_next"] = cursor_next
            posts.append(post)
        return posts

    # ---------------------------------------------------------------- подписчики
    def followers(self, handle):
        """Подписчики аккаунта (ТЗ-44) или ``None``, если профиль не найден."""
        prof = self.profile(handle)
        if not prof:
            return None
        return {"id": prof.get("id"), "handle": prof.get("handle"),
                "followers": prof.get("followers"), "tweets": prof.get("tweets")}

    def close(self):
        if self._owns_con:
            try:
                self._con.close()
            except Exception:
                pass


# --------------------------------------------------------------- синглтон
_SESSION = None
_SESSION_LAST_ERROR = None


def get_session_transport(**kwargs):
    """Процессный синглтон сессионного транспорта.

    Возвращает ``None``, если сессия недоступна: вызывающий обязан продолжить
    работу на Nitter (признак отсутствия сессии НЕ отказ Nitter).
    """
    global _SESSION, _SESSION_LAST_ERROR
    if _SESSION is None:
        try:
            _SESSION = XSessionTransport(**kwargs)
            _SESSION_LAST_ERROR = None
        except Exception as e:  # noqa: BLE001 — любой сбой = работаем на Nitter
            _SESSION_LAST_ERROR = f"{type(e).__name__}: {e}"
            return None
    return _SESSION


def session_last_error():
    return _SESSION_LAST_ERROR


def reset_session_transport():
    global _SESSION, _SESSION_LAST_ERROR
    if _SESSION is not None:
        _SESSION.close()
    _SESSION = None
    _SESSION_LAST_ERROR = None
