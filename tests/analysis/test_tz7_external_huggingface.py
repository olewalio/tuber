"""ТЗ-7: Hugging Face во внешнем контуре новинок.

Два фитчера в :mod:`tuber.analysis.external` — трендовые модели
(``/api/models``) и статьи дня (``/api/daily_papers``). Проверяем на коротких
вырезках РЕАЛЬНЫХ ответов API (сняты 08.10.2026), сеть не зовётся:

* разбор моделей: число записей, ``platform``/``url``/``score``, окно по
  ``createdAt``, ``name_text`` без автора (дефисы/подчёркивания → пробелы);
* разбор статей дня: ``url`` вида ``…/papers/<id>``, ``score`` из ``upvotes``,
  окно по ``paper.publishedAt``;
* битый JSON → ``parse_error`` и не роняет остальные источники;
* отказ одного HF-источника из двух → второй всё равно собирается.
"""
from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

from tuber.analysis import external

#: Реальные вырезки ответов (2–3 элемента) — поля, которые нам нужны.
HF_MODELS_JSON = json.dumps([
    {
        "id": "autotrust/JEV-27B-VL",
        "likes": 2779,
        "downloads": 1533034,
        "trendingScore": 2308,
        "createdAt": "2026-09-30T05:02:23.000Z",
        "pipeline_tag": "image-text-to-text",
    },
    {
        "id": "Cloudflare/clef",
        "likes": 1875,
        "downloads": 10874,
        "trendingScore": 1581,
        "createdAt": "2026-09-30T21:15:35.000Z",
        "pipeline_tag": "image-text-to-text",
    },
    {
        "id": "autotrust/GEV-26B-Decide",
        "likes": 1766,
        "downloads": 903866,
        "trendingScore": 1508,
        "createdAt": "2026-10-02T04:20:21.000Z",
        "pipeline_tag": "text-classification",
    },
])

HF_PAPERS_JSON = json.dumps([
    {
        "title": "How corner is a corner case? Percentile control for highway safety",
        "publishedAt": "2026-10-03T20:00:00.000Z",
        "numComments": 2,
        "paper": {
            "id": "2610.05003",
            "title": "How corner is a corner case? Percentile control for highway safety",
            "publishedAt": "2026-10-04T00:00:00.000Z",
            "upvotes": 0,
        },
    },
    {
        "title": "MIMESIS: Learning User Simulators as Training Environments",
        "publishedAt": "2026-10-06T20:00:00.000Z",
        "numComments": 1,
        "paper": {
            "id": "2610.09484",
            "title": "MIMESIS: Learning User Simulators as Training Environments",
            "publishedAt": "2026-10-07T00:00:00.000Z",
            "upvotes": 4,
        },
    },
    {
        "title": "Iris-3B: Going Beyond the Latent with Pixel-Space Diffusion",
        "publishedAt": "2026-10-06T20:00:00.000Z",
        "numComments": 3,
        "paper": {
            "id": "2610.09450",
            "title": "Iris-3B: Going Beyond the Latent with Pixel-Space Diffusion",
            "publishedAt": "2026-10-07T00:00:00.000Z",
            "upvotes": 6,
        },
    },
])

#: Якоря: модели — 02.10.2026 12:00Z (окно 48ч → отсекает JEV от 30.09),
#: статьи — 07.10.2026 12:00Z (отсекает 2610.05003 от 04.10).
MODELS_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
PAPERS_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class _FakeClient:
    """Подмена ``HttpClient``: карта URL → тело или исключение."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[str] = []

    def get(self, url, headers=None):
        self.calls.append(url)
        value = self.mapping.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"неожиданный URL в тесте: {url}")
        return value


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------

def test_models_parse_fields_and_window():
    client = _FakeClient({external.HF_MODELS_URL: HF_MODELS_JSON})
    items = external.fetch_huggingface_models(client, now=MODELS_NOW, window_hours=48)
    # JEV (30.09 05:02) вне окна, остальные два — внутри.
    assert len(items) == 2
    assert all(it["platform"] == "huggingface" for it in items)
    first = items[0]
    assert first["url"] == "https://huggingface.co/Cloudflare/clef"
    assert first["score"] == 1581
    # имя без автора, дефисы/подчёркивания → пробелы
    assert first["name_text"] == "clef"
    second = items[1]
    assert second["name_text"] == "GEV 26B Decide"
    assert second["published_at"] == "2026-10-02 04:20:21"
    assert all(it["url"] != "https://huggingface.co/autotrust/JEV-27B-VL"
               for it in items)


def test_models_respect_max_items(monkeypatch):
    monkeypatch.setattr(external, "HF_MODELS_MAX_ITEMS", 1)
    client = _FakeClient({external.HF_MODELS_URL: HF_MODELS_JSON})
    items = external.fetch_huggingface_models(client, now=MODELS_NOW, window_hours=48)
    assert len(items) == 1


# ---------------------------------------------------------------------------
# Статьи дня
# ---------------------------------------------------------------------------

def test_papers_parse_fields_and_window():
    client = _FakeClient({external.HF_PAPERS_URL: HF_PAPERS_JSON})
    items = external.fetch_huggingface_papers(client, now=PAPERS_NOW, window_hours=48)
    # 2610.05003 (04.10) вне окна, остальные два — внутри.
    assert len(items) == 2
    assert all(it["platform"] == "huggingface" for it in items)
    assert items[0]["url"] == "https://huggingface.co/papers/2610.09484"
    assert items[0]["score"] == 4
    assert items[1]["url"] == "https://huggingface.co/papers/2610.09450"
    assert items[1]["score"] == 6
    assert all("2610.05003" not in (it["url"] or "") for it in items)
    # комментарии не выдумываем
    assert all(it["comments"] is None for it in items)


def test_papers_fallback_to_top_level_published():
    body = json.dumps([{
        "title": "Top-level dated paper",
        "publishedAt": "2026-10-07T00:00:00.000Z",
        "paper": {"id": "2610.99999", "title": "Top-level dated paper"},
    }])
    client = _FakeClient({external.HF_PAPERS_URL: body})
    items = external.fetch_huggingface_papers(client, now=PAPERS_NOW, window_hours=48)
    assert len(items) == 1
    assert items[0]["published_at"] == "2026-10-07 00:00:00"
    assert items[0]["url"] == "https://huggingface.co/papers/2610.99999"
    assert items[0]["score"] is None


# ---------------------------------------------------------------------------
# Отказы источников
# ---------------------------------------------------------------------------

def test_bad_json_is_parse_error_and_others_survive():
    client = _FakeClient({
        external.HF_MODELS_URL: HF_MODELS_JSON,
        external.HF_PAPERS_URL: "{ это не JSON",
    })
    result = external.collect(
        now=MODELS_NOW, window_hours=48, client=client,
        sources=("huggingface_models", "huggingface_papers"))
    assert result["sources"]["huggingface_models"]["status"] == external.SOURCE_OK
    assert result["sources"]["huggingface_models"]["count"] == 2
    assert result["sources"]["huggingface_papers"]["status"] == \
        external.SOURCE_PARSE_ERROR
    assert len(result["items"]) == 2  # модели собрались, бумаги — нет


def test_one_hf_source_down_second_still_collected():
    err = urllib.error.HTTPError(external.HF_MODELS_URL, 503, "unavailable", {}, None)
    client = _FakeClient({
        external.HF_MODELS_URL: err,
        external.HF_PAPERS_URL: HF_PAPERS_JSON,
    })
    result = external.collect(
        now=PAPERS_NOW, window_hours=48, client=client,
        sources=("huggingface_models", "huggingface_papers"))
    assert result["sources"]["huggingface_models"]["status"] == \
        external.SOURCE_HTTP_ERROR
    assert result["sources"]["huggingface_models"]["count"] == 0
    assert result["sources"]["huggingface_papers"]["status"] == external.SOURCE_OK
    assert result["sources"]["huggingface_papers"]["count"] == 2
    assert len(result["items"]) == 2
    assert all(it["platform"] == "huggingface" for it in result["items"])


def test_empty_arrays_are_ok():
    client = _FakeClient({
        external.HF_MODELS_URL: "[]",
        external.HF_PAPERS_URL: "[]",
    })
    result = external.collect(
        now=MODELS_NOW, window_hours=48, client=client,
        sources=("huggingface_models", "huggingface_papers"))
    assert result["sources"]["huggingface_models"]["status"] == external.SOURCE_OK
    assert result["sources"]["huggingface_models"]["count"] == 0
    assert result["sources"]["huggingface_papers"]["status"] == external.SOURCE_OK
    assert result["items"] == []


# ---------------------------------------------------------------------------
# Реестры
# ---------------------------------------------------------------------------

def test_registry_and_platforms():
    assert external.PLATFORMS["huggingface"] == "Hugging Face"
    assert external.FETCHERS["huggingface_models"] is external.fetch_huggingface_models
    assert external.FETCHERS["huggingface_papers"] is external.fetch_huggingface_papers


def test_huggingface_item_is_release():
    from tuber.analysis import novelties
    assert novelties.is_release_item({"platform": "huggingface", "title": "clef"})
    assert novelties.is_release_item({"platform": "huggingface", "title": "Iris-3B"})
