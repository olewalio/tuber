"""ТЗ-8 Р1.2-Р1.4: нормализация цели ребра (единая точка правды)."""
from __future__ import annotations

from tuber.core import graph


def test_x_account_and_service_paths():
    t = graph.classify_target("https://x.com/Foo")
    assert (t.target_type, t.target_platform, t.target_value) == ("account", "x", "foo")
    t = graph.classify_target("https://twitter.com/Bar?ref_src=twsrc")
    assert t.target_value == "bar" and t.skip_reason is None
    assert graph.classify_target("https://x.com/i/flow/login").skip_reason == "service_path"
    assert graph.classify_target("https://x.com/u/status/123").skip_reason == "service_path"
    assert graph.classify_target("https://x.com/search?q=ai").skip_reason == "service_path"


def test_telegram_channels_and_invites():
    for url in ("https://t.me/SomeChan", "https://t.me/s/SomeChan?start=1",
                "https://telegram.dog/SomeChan", "https://telegram.me/SomeChan"):
        t = graph.classify_target(url)
        assert (t.target_type, t.target_platform, t.target_value) == (
            "channel", "telegram", "somechan"), url
    assert graph.classify_target("https://t.me/+AbCdEf").skip_reason == "invite"
    assert graph.classify_target("https://t.me/").skip_reason == "no_target"


def test_youtube_video_channel_and_mirror():
    t = graph.classify_target("https://youtu.be/abcdefghijk")
    assert (t.target_type, t.target_platform, t.target_value) == ("video", "youtube", "abcdefghijk")
    t = graph.classify_target("https://www.youtube.com/watch?v=abcdefghijk")
    assert t.target_type == "video" and t.target_value == "abcdefghijk"
    t = graph.classify_target("https://youtube.com/shorts/abcdefghijk")
    assert t.target_type == "video"
    t = graph.classify_target("https://youtube.com/@SomeChannel")
    assert (t.target_type, t.target_value) == ("channel", "somechannel")
    t = graph.classify_target("https://youtube.com/channel/UCabcdefghijklmnopqrstuv")
    assert t.target_value == "UCabcdefghijklmnopqrstuv"
    t = graph.classify_target("https://piped.video/watch?v=abcdefghijk")
    assert t.target_platform == "youtube" and t.target_type == "video"
    # служебные пути YouTube не становятся ложным каналом (найдено на живой базе)
    assert graph.classify_target("https://youtube.com/playlist?list=PL123").skip_reason == "service_path"
    assert graph.classify_target("https://youtube.com/results?search_query=x").skip_reason == "service_path"


def test_domain_normalization_and_utm_strip():
    t = graph.classify_target("https://WWW.Example.COM/path?a=1&utm_source=x&ref=go&fbclid=z")
    assert (t.target_type, t.target_platform, t.target_value) == ("domain", "web", "example.com")
    assert "utm_source" not in t.target_url and "fbclid" not in t.target_url
    assert "a=1" in t.target_url


def test_skip_hosts_and_competitors():
    assert graph.classify_target("https://bit.ly/x").skip_reason == "skip_host"
    assert graph.classify_target("https://t.co/x").skip_reason == "skip_host"
    assert graph.classify_target("https://docs.google.com/doc").skip_reason == "skip_host"
    assert graph.classify_target("https://nitter.net/foo").skip_reason == "skip_host"
    t = graph.classify_target("https://max.ru/foo")
    assert t.skip_reason is None and t.competitor == 1 and t.target_value == "max.ru"
    t = graph.classify_target("https://vk.com/foo")
    assert t.competitor == 1


def test_normalize_edge_target_mention_and_url():
    t = graph.normalize_edge_target("mention", "@SomeUser")
    assert t.target_type == "account" and t.target_value == "someuser"
    assert graph.normalize_edge_target("mention", "") is None
    assert graph.normalize_edge_target("link_web", "https://x.com/foo").target_value == "foo"
    assert graph.normalize_edge_target("link_web", "https://bit.ly/x") is None


def test_edge_kind_for_targets():
    assert graph.edge_kind_for(graph.classify_target("https://x.com/foo")) == "link_x"
    assert graph.edge_kind_for(graph.classify_target("https://t.me/foo")) == "link_tg"
    assert graph.edge_kind_for(graph.classify_target("https://youtu.be/abcdefghijk")) == "link_yt"
    assert graph.edge_kind_for(graph.classify_target("https://habr.com/x")) == "link_web"


def test_broken_urls_do_not_raise():
    """ТЗ №2 ч.2: битый URL не роняет разбор (ValueError: Invalid IPv6 URL)."""
    samples = ("https://[::1", "http://[bad",
               "ok https://example.com/a", "[текст](https://x.com/a)")
    for raw in samples:
        cleaned, parsed = graph._clean_url(raw)  # не бросает
        assert isinstance(cleaned, str)
        assert parsed is not None
    # Ссылка с несбалансированной скобкой отбрасывается как bad_url, не исключение.
    assert graph.classify_target("https://[::1").skip_reason == "bad_url"
    assert graph.classify_target("[текст](https://x.com/a)").skip_reason is not None
