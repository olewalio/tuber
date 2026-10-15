"""Генератор заголовка ``x-client-transaction-id`` для внутреннего GraphQL X.

Перенос алгоритма из ``twscrape/xclid.py`` (проект vladkens/twscrape, лицензия
MIT) и, через него, из ``XClientTransaction`` за авторством @SarabjitDhiman
(также MIT). Ссылки с разбором алгоритма:
https://antibot.blog/posts/1741552025433 , .../1741552092462 , .../1741552163416

Зачем перенос: собственный клиент проекта (``tuber/platforms/x/session.py``)
получает ``http=404`` на GraphQL-операции (в частности ``SearchTimeline``) без
этого заголовка и ``200`` с ним (проверено вживую 07.10.2026; причина — issue
twscrape #248). Оригинал при 404 генерирует новый заголовок и повторяет запрос.

Отличия от оригинала (обязательные для проекта):

1. Код СИНХРОННЫЙ (проект синхронный), без ``async/await``.
2. НИКАКОЙ своей сети: текст страниц/скриптов добывает переданная снаружи
   функция ``fetch(url) -> str``. Так соблюдается инвариант «весь выход в сеть —
   через :func:`tuber.platforms.x.broker.raw_http_get`»: сетевую часть даёт
   вызывающий (``XSessionTransport``), а не эта библиотека.
3. Зависимости: только стандартная библиотека + ``bs4``.

Публичный интерфейс:

* :func:`create` — построить генератор, добрав вёрстку из ``fetch``;
* :meth:`XClIdGen.calc` — сам заголовок по методу и пути;
* :meth:`XClIdGen.keys` — ``(vk_bytes, anim_key)`` для кэша вызывающего;
* конструктор ``XClIdGen(vk_bytes, anim_key)`` — восстановление из кэша.

``time``/``random`` — модульные: тесты подменяют их через ``monkeypatch``.
"""
from __future__ import annotations

import base64
import hashlib
import math
import random
import re
import time
from urllib.parse import urljoin

import bs4


class XClIdError(Exception):
    """Базовая ошибка генератора ``x-client-transaction-id``."""


class XClIdParseError(XClIdError):
    """Вёрстка X не дала нужных данных (ключ, анимация или индексы)."""


# ------------------------------------------------------------- разбор вёрстки
# Текущая сборка X (Vite): скрипты слинкованы прямо в HTML.
ASSET_URL_RE = re.compile(r"https://[\w.-]+/x-web/[\w./-]+\.js")
RESPONSIVE_WEB_URL_RE = re.compile(
    r"https://[\w.-]+/responsive-web/client-web/[\w./-]+\.js")
LEGACY_MAIN_RE = re.compile(r"/client-web/main\.([^.\"']+)\.js")
LOGGED_OUT_ENTRY_RE = re.compile(
    r"(?:^|/)entry-client-logged-out(?:[-.][^/?#]+)?\.js(?:[?#].*)?$")

# Файл с индексами анимации: legacy — ``ondemand.s.*.js``, текущая сборка —
# динамически импортируемый ``sign.o-*.js``. ``\b`` отсекает ``design.o-*.js``.
INDICES_FILE_RE = re.compile(
    r"(?:\.{0,2}/)?[\w./-]*?\b(?:ondemand\.s|sign\.o)[\w.-]*\.js")

INDICES_REGEX = re.compile(r"(\(\w{1}\[(\d{1,2})\],\s*16\))+",
                           flags=(re.VERBOSE | re.MULTILINE))


def script_url(k: str, v: str) -> str:
    return f"https://abs.twimg.com/responsive-web/client-web/{k}.{v}.js"


def get_scripts_list(text: str) -> list[str]:
    """Собрать URL скриптов X из HTML (x-web, responsive-web и legacy webpack).

    Legacy-сборка прячет две карты: ``{chunk_id: "hex"}`` и
    ``{chunk_id: "имя"}``; URL собирается как ``{имя}.{hex}a.js``.
    """
    urls = ASSET_URL_RE.findall(text) + RESPONSIVE_WEB_URL_RE.findall(text)
    if main_match := LEGACY_MAIN_RE.search(text):
        urls.append(script_url("main", main_match.group(1)))

    # Карта хэшей: значения ровно 7 или 16 строчных hex-цифр.
    hash_map = {
        m.group(1): m.group(2)
        for m in re.finditer(r'(\d+):"([0-9a-f]{7}|[0-9a-f]{16})"', text)
    }
    # Карта имён: значения НЕ подходят под hex-шаблон выше.
    name_map: dict[str, str] = {}
    for m in re.finditer(r'(\d+):"([^"]+)"', text):
        val = m.group(2)
        if not re.fullmatch(r"[0-9a-f]{7}|[0-9a-f]{16}", val):
            name_map[m.group(1)] = val

    urls.extend(
        script_url(name_map.get(chunk_id, chunk_id), hash_val + "a")
        for chunk_id, hash_val in hash_map.items()
    )
    if not urls:
        raise XClIdParseError("X web scripts not found")
    return list(dict.fromkeys(urls))


def _get_text(url: str, fetch) -> str:
    """Тело страницы по URL через ``fetch``; поддержан GET-редирект X.

    ``fetch(url) -> str`` возвращает тело или бросает исключение. Оригинал
    дополнительно умеет POST ``/x/migrate`` для гостевых сессий; здесь POST
    недоступен по построению (``fetch`` — только GET) и не нужен: страницу
    тянет авторизованная сессия. При неожиданной форме миграции честно падаем.
    """
    text = fetch(url)
    if ">document.location =" not in text:
        return text
    url = text.split('document.location = "')[1].split('"')[0]
    text = fetch(url)
    if 'action="https://x.com/x/migrate" method="post"' in text:
        raise XClIdParseError("X web page requires POST migrate (unsupported)")
    return text


def _find_indices_url(fetch, scripts: list[str]) -> str:
    """Первый скрипт, ссылающийся на файл индексов анимации.

    Оригинал сканирует асинхронно; здесь последовательный проход (сеть у
    вызывающего, набор скриптов невелик).
    """
    loaded = failed = 0
    for url in scripts:
        try:
            body = fetch(url)
        except Exception:  # noqa: BLE001 — недоставший скрипт не фатален
            failed += 1
            continue
        loaded += 1
        m = INDICES_FILE_RE.search(body)
        if m:
            return urljoin(url, m.group(0))
    raise XClIdParseError(
        f"Signing script not found (assets: {loaded} loaded, {failed} failed)")


def parse_anim_idx(text: str, fetch) -> list[int]:
    """Индексы анимации из JS файла индексов (сеть — через ``fetch``)."""
    scripts = get_scripts_list(text)
    x_web_scripts = [url for url in scripts if "/x-web/" in url]
    if x_web_scripts:
        if any(LOGGED_OUT_ENTRY_RE.search(url) for url in x_web_scripts):
            raise XClIdParseError("Logged-out X web app")
        scripts = x_web_scripts

    direct = [x for x in scripts if INDICES_FILE_RE.search(x)]
    url = direct[0] if direct else _find_indices_url(fetch, scripts)

    body = _get_text(url, fetch)
    items = [int(x.group(2)) for x in INDICES_REGEX.finditer(body)]
    if not items:
        raise XClIdParseError("Signing indices not found")
    return items


def parse_vk_bytes(soup: bs4.BeautifulSoup) -> list[int]:
    """Верификационный ключ X (base64 в ``meta twitter-site-verification``)."""
    el = soup.find("meta", {"name": "twitter-site-verification", "content": True})
    el = str(el.get("content")) if el and isinstance(el, bs4.Tag) else None
    if not el:
        raise XClIdParseError("X verification key not found")
    try:
        return list(base64.b64decode(bytes(el, "utf-8"), validate=True))
    except ValueError as e:
        raise XClIdParseError("Invalid X verification key") from e


def parse_anim_arr(soup: bs4.BeautifulSoup, vk_bytes: list[int]) -> list[list[float]]:
    """Матрица анимации из SVG ``loading-x-anim`` (список строк чисел)."""
    els = list(soup.select("svg[id^='loading-x-anim'] g:first-child path:nth-child(2)"))
    els = [str(x.get("d") or "").strip() for x in els]
    if not els:
        raise XClIdParseError("Animation data not found")
    idx = vk_bytes[5] % len(els)
    dat = els[idx][9:].split("C")
    try:
        return [list(map(float, re.sub(r"[^\d]+", " ", x).split())) for x in dat]
    except (IndexError, ValueError) as e:
        raise XClIdParseError("Invalid animation data") from e


# ------------------------------------------------- математика анимации (MIT)
class Cubic:
    def __init__(self, curves: list[float]):
        self.curves = curves

    def get_value(self, time: float) -> float:
        start_gradient = end_gradient = start = mid = 0.0
        end = 1.0

        if time <= 0.0:
            if self.curves[0] > 0.0:
                start_gradient = self.curves[1] / self.curves[0]
            elif self.curves[1] == 0.0 and self.curves[2] > 0.0:
                start_gradient = self.curves[3] / self.curves[2]
            return start_gradient * time

        if time >= 1.0:
            if self.curves[2] < 1.0:
                end_gradient = (self.curves[3] - 1.0) / (self.curves[2] - 1.0)
            elif self.curves[2] == 1.0 and self.curves[0] < 1.0:
                end_gradient = (self.curves[1] - 1.0) / (self.curves[0] - 1.0)
            return 1.0 + end_gradient * (time - 1.0)

        while start < end:
            mid = (start + end) / 2
            x_est = self.calculate(self.curves[0], self.curves[2], mid)
            if abs(time - x_est) < 0.00001:
                return self.calculate(self.curves[1], self.curves[3], mid)
            if x_est < time:
                start = mid
            else:
                end = mid
        return self.calculate(self.curves[1], self.curves[3], mid)

    @staticmethod
    def calculate(a: float, b: float, m: float) -> float:
        return 3.0 * a * (1 - m) * (1 - m) * m + 3.0 * b * (1 - m) * m * m + m * m * m


def interpolate(from_list: list[float], to_list: list[float], f: float):
    assert len(from_list) == len(to_list), \
        f"Mismatched interpolation args {from_list}: {to_list}"
    return [a * (1 - f) + b * f for a, b in zip(from_list, to_list)]


def get_rotation_matrix(rotation: float):
    rad = math.radians(rotation)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def solve(value: float, min_val: float, max_val: float, rounding: bool):
    result = value * (max_val - min_val) / 255 + min_val
    return math.floor(result) if rounding else round(result, 2)


def float_to_hex(x):
    result = []
    quotient = int(x)
    fraction = x - quotient

    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))
        if remainder > 9:
            result.insert(0, chr(remainder + 55))
        else:
            result.insert(0, str(remainder))
        x = float(quotient)

    if fraction == 0:
        return "".join(result)

    result.append(".")
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)
        if integer > 9:
            result.append(chr(integer + 55))
        else:
            result.append(str(integer))
    return "".join(result)


def cacl_anim_key(frames: list[float], target_time: float) -> str:
    """Ключ анимации из строки кадров SVG (обфусцированный hex-подобный)."""
    from_color = [*frames[:3], 1]
    to_color = [*frames[3:6], 1]
    from_rotation = [0.0]
    to_rotation = [solve(frames[6], 60.0, 360.0, True)]

    frames = frames[7:]
    curves = [solve(x, -1.0 if i % 2 else 0.0, 1.0, False) for i, x in enumerate(frames)]
    val = Cubic(curves).get_value(target_time)

    color = interpolate(from_color, to_color, val)
    color = [max(0, min(255, value)) for value in color]
    rotation = interpolate(from_rotation, to_rotation, val)

    matrix = get_rotation_matrix(rotation[0])
    str_arr = [format(round(value), "x") for value in color[:-1]]
    for value in matrix:
        rounded = round(value, 2)
        if rounded < 0:
            rounded = -rounded
        hex_value = float_to_hex(rounded)
        str_arr.append(
            f"0{hex_value}".lower() if hex_value.startswith(".")
            else hex_value if hex_value else "0"
        )
    str_arr.extend(["0", "0"])
    return re.sub(r"[.-]", "", "".join(str_arr))


def load_keys(soup: bs4.BeautifulSoup, fetch) -> tuple[list[int], str]:
    """Собрать ``(vk_bytes, anim_key)`` из страницы.

    Порядок отличается от оригинала: локальные (без сети) разборы ключа и
    анимации идут ПЕРВЫМИ, и только затем скачиваются индексы. Если страница не
    похожа на нужную (нет ключа/анимации), сеть не дёргается вовсе — так
    сломанная страница не порождает лишних запросов к X.
    """
    vk_bytes = parse_vk_bytes(soup)
    anim_arr = parse_anim_arr(soup, vk_bytes)
    anim_idx = parse_anim_idx(str(soup), fetch)

    frame_time = 1
    for x in anim_idx[1:]:
        frame_time *= vk_bytes[x] % 16
    frame_time = math.floor(frame_time / 10 + 0.5) * 10  # JS Math.round до 10

    frame_idx = vk_bytes[anim_idx[0]] % 16
    frame_row = anim_arr[frame_idx]
    frame_dur = float(frame_time) / 4096

    anim_key = cacl_anim_key(frame_row, frame_dur)
    return vk_bytes, anim_key


class XClIdGen:
    """Держит ``(vk_bytes, anim_key)`` и считает заголовок по методу и пути."""

    def __init__(self, vk_bytes: list[int], anim_key: str):
        self.vk_bytes = vk_bytes
        self.anim_key = anim_key

    def keys(self) -> tuple[list[int], str]:
        """Данные для кэша вызывающего."""
        return self.vk_bytes, self.anim_key

    def calc(self, method: str, path: str) -> str:
        """Заголовок ``x-client-transaction-id`` для ``method``/``path``.

        ``path`` — только путь URL (без query), как в оригинале:
        ``urlparse(url).path or "/"``.
        """
        ts = math.floor((time.time() * 1000 - 1682924400 * 1000) / 1000)
        ts_bytes = [(ts >> (i * 8)) & 0xFF for i in range(4)]

        dkw, drn = "obfiowerehiring", 3  # default keyword and random number
        pld = f"{method.upper()}!{path}!{ts}{dkw}{self.anim_key}"
        pld = list(hashlib.sha256(pld.encode()).digest())
        pld = [*self.vk_bytes, *ts_bytes, *pld[:16], drn]

        num = random.randint(0, 255)
        pld = bytearray([num, *[x ^ num for x in pld]])
        out = base64.b64encode(pld).decode("utf-8").strip("=")
        return out


def create(fetch, *, cookies: bool = True) -> XClIdGen:
    """Построить :class:`XClIdGen`, добрав вёрстку X через ``fetch``.

    ``fetch(url) -> str`` даёт тело ответа (или бросает исключение). Аргумент
    ``cookies`` сохранён для совместимости с оригиналом (там он включал куки в
    HTTP-клиент); здесь куки — забота ``fetch``, поэтому значение не влияет.
    """
    text = _get_text("https://x.com/tesla", fetch)
    soup = bs4.BeautifulSoup(text, "html.parser")
    vk_bytes, anim_key = load_keys(soup, fetch)
    return XClIdGen(vk_bytes, anim_key)
