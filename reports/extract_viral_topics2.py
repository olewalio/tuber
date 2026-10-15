#!/usr/bin/env python3
# ПРОБА v2 (read-only): виральные материалы за окно, БЕЗ Маска/Альтмана, только ИИ-контур.
# Метрики: X — значимость + кратность к медиане лайков автора; YouTube — просмотры + кратность к медиане канала;
# Telegram — кратность к норме канала (только каналы с плотной ИИ-разметкой).
import sqlite3, statistics, json, sys, re
from datetime import datetime, timedelta, timezone

DB = '/root/tuber/data/tuber.db'
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
NOW = datetime.now(timezone.utc)
START = (NOW - timedelta(days=DAYS)).strftime('%Y-%m-%d %H:%M:%S')
B30 = (NOW - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
con.row_factory = sqlite3.Row
q = con.execute

def owner(meta):
    try:
        return (json.loads(meta or '{}') or {}).get('owner_handle') or ''
    except Exception:
        return ''

MUSK_HANDLES = {'elonmusk', 'sama', 'openai', 'grok', 'xai'}
MUSK_WORDS = ('musk', 'маск', 'altman', 'альтман')

def muskish(handle, text):
    if (handle or '').lower() in MUSK_HANDLES:
        return True
    t = (text or '').lower()
    return any(w in t for w in MUSK_WORDS)

# ================= X =================
# title_ru/summary_ru берём из classification через LEFT JOIN: у ~54 постов окна
# разметка есть, но перевод пуст — их нельзя терять, для них downstream берётся
# сырой текст. Отбор остаётся прежним: только is_ai=1 (WHERE, как раньше в join).
xs = q("""select c.id, c.url, c.published_at, c.text, c.meta_json, cl.likes, cl.replies, cl.views,
                 cs.topic, cs.is_ai, cs.title_ru, cs.summary_ru, s.handle src_handle
          from content c
          left join content_latest cl on cl.content_id=c.id
          left join classification cs on cs.content_id=c.id
          join source s on s.id=c.source_id
          where c.platform='x' and c.published_at>=? and c.is_repost=0 and cs.is_ai=1""", (START,)).fetchall()

xbase = {}
for r in q("""select c.meta_json, cl.likes from content c
              join content_latest cl on cl.content_id=c.id
              where c.platform='x' and c.published_at>=? and c.is_repost=0 and cl.likes is not null""", (B30,)):
    h = owner(r['meta_json']).lower()
    if h:
        xbase.setdefault(h, []).append(r['likes'])

x_out, x_dropped = [], []
for r in xs:
    if r['likes'] is None:
        continue
    h = owner(r['meta_json']) or r['src_handle']
    rec = {'handle': h, 'src': r['src_handle'], 'likes': r['likes'], 'replies': r['replies'],
           'at': r['published_at'], 'url': r['url'], 'topic': r['topic'],
           'title_ru': r['title_ru'] or '', 'summary_ru': r['summary_ru'] or '',
           'text': (r['text'] or '').replace('\n', ' ')[:300]}
    if muskish(h, r['text']):
        x_dropped.append(rec)
        continue
    base = xbase.get(h.lower(), [])
    rec['med'] = int(statistics.median(base)) if len(base) >= 5 else None
    rec['ratio'] = round(r['likes'] / rec['med'], 1) if rec['med'] else None
    rec['n_base'] = len(base)
    age = max((NOW - datetime.strptime(r['published_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)).total_seconds() / 3600, 0.5)
    rec['per_h'] = int(r['likes'] / min(age, 48)); rec['age_h'] = round(age, 1)
    x_out.append(rec)

# ================= YouTube =================
ys = q("""select c.id, c.url, c.title, c.lang, c.published_at, s.handle src, s.subs,
                 ms.views, ms.likes, ms.captured_at, cs.title_ru, cs.summary_ru, cs.topic
          from content c
          join source s on s.id=c.source_id
          join classification cs on cs.content_id=c.id and cs.is_ai=1
          join (select content_id x, max(captured_at) cap from metric_snapshot where platform='youtube' group by 1) t on t.x=c.id
          join metric_snapshot ms on ms.content_id=c.id and ms.captured_at=t.cap
          where c.platform='youtube' and c.published_at>=? and ms.views is not null""", (START,)).fetchall()

ybase = {}
for r in q("""select s.handle h, ms.views v from content c join source s on s.id=c.source_id
              join (select content_id x, max(captured_at) cap from metric_snapshot where platform='youtube' group by 1) t on t.x=c.id
              join metric_snapshot ms on ms.content_id=c.id and ms.captured_at=t.cap
              where c.platform='youtube' and c.published_at>=? and ms.views is not null""", (B30,)):
    if r['v'] is not None:
        ybase.setdefault(r['h'], []).append(r['v'])

y_out, y_dropped = [], []
for r in ys:
    rec = {'src': r['src'], 'views': r['views'], 'likes': r['likes'], 'subs': r['subs'],
           'lang': r['lang'], 'title': r['title'] or '',
           'at': r['published_at'], 'url': r['url'], 'topic': r['topic'],
           'title_ru': r['title_ru'] or r['title'],
           'reply': round(100.0 * (r['likes'] or 0) / r['views'], 2) if r['views'] else None,
           'reach': round(r['views'] / r['subs'], 1) if r['subs'] else None, 'cap': r['captured_at']}
    if muskish(r['src'], (r['title'] or '') + (r['title_ru'] or '')):
        y_dropped.append(rec); continue
    base = ybase.get(r['src'], [])
    rec['med'] = int(statistics.median(base)) if len(base) >= 10 else None
    rec['ratio'] = round(r['views'] / rec['med'], 1) if rec['med'] else None
    y_out.append(rec)

# ================= Telegram: только каналы с плотной ИИ-разметкой =================
tg = q("""select c.id, c.url, c.lang, c.published_at, c.text, s.handle src, cl.views, cl.forwards, cl.reactions,
                 cs.is_ai
          from content c join source s on s.id=c.source_id
          left join content_latest cl on cl.content_id=c.id
          left join classification cs on cs.content_id=c.id
          where c.platform='telegram' and c.published_at>=? and cl.views is not null""", (START,)).fetchall()

ch_stat = {}
tbase = {}
for r in q("""select s.handle h, cl.views v, cs.is_ai ai from content c join source s on s.id=c.source_id
              join content_latest cl on cl.content_id=c.id
              left join classification cs on cs.content_id=c.id
              where c.platform='telegram' and c.published_at>=? and cl.views is not null""", (B30,)):
    tbase.setdefault(r['h'], []).append(r['v'])
    if r['ai'] is not None:
        st = ch_stat.setdefault(r['h'], [0, 0]); st[0] += 1; st[1] += r['ai']

ai_channels = {h for h, (n, ai) in ch_stat.items() if n >= 5 and ai / n >= 0.35}
t_out = []
for r in tg:
    if r['src'] not in ai_channels:
        continue
    if muskish(r['src'], r['text']):
        continue
    base = tbase.get(r['src'], [])
    med = statistics.median(base) if len(base) >= 20 else None
    t_out.append({'src': r['src'], 'views': r['views'], 'forwards': r['forwards'], 'reactions': r['reactions'],
                  'lang': r['lang'],
                  'med': int(med) if med else None, 'ratio': round(r['views'] / med, 1) if med else None,
                  'at': r['published_at'], 'url': r['url'], 'text': (r['text'] or '').replace('\n', ' ')[:250]})

data = {'window_days': DAYS, 'start': START, 'now': str(NOW)[:19],
        'ai_channels': sorted(ai_channels), 'x': x_out, 'youtube': y_out, 'telegram': t_out,
        'dropped_count': {'x': len(x_dropped), 'youtube': len(y_dropped)},
        'dropped_top': sorted(x_dropped, key=lambda r: -r['likes'])[:5]}
json.dump(data, open('/tmp/viral2.json', 'w'), ensure_ascii=False)

L = []
L.append(f"окно {DAYS} сут: {START} → {str(NOW)[:19]} UTC")
L.append(f"пулы после фильтра is_ai=1: X={len(x_out)} YT={len(y_out)}; ИИ-каналов TG={len(ai_channels)}")
L.append(f"выброшено Маск/Альтман: X={len(x_dropped)} YT={len(y_dropped)}")
L.append(f"ТГ-каналы ИИ: {', '.join(sorted(ai_channels))}")
L.append("")
L.append("### X — по КРАТНОСТИ к медиане автора (лайки>=300, база>=5 постов)")
for r in sorted([r for r in x_out if r['ratio'] and r['likes'] >= 300], key=lambda r: -r['ratio'])[:20]:
    L.append(f"{r['ratio']}x (медиана {r['med']}, база {r['n_base']}) | {r['likes']} лайков | {r['replies']} отв | {int(r['per_h'])}/ч | @{r['handle']} | {r['at']} | тема={r['topic']}")
    L.append(f"   {r['url']}")
    L.append(f"   {r['text'][:200]}")
L.append("")
L.append("### X — по АБСОЛЮТУ лайков (топ-15, без Маска/Альтмана)")
for r in sorted(x_out, key=lambda r: -r['likes'])[:15]:
    L.append(f"{r['likes']} лайков | {r['replies']} отв | {int(r['per_h'])}/ч | ratio={r['ratio']} | @{r['handle']} | {r['at']} | тема={r['topic']}")
    L.append(f"   {r['url']} | {r['text'][:160]}")
L.append("")
L.append("### YouTube — по КРАТНОСТИ к медиане канала (просм>=20000)")
for r in sorted([r for r in y_out if r['ratio'] and r['views'] >= 20000], key=lambda r: -r['ratio'])[:20]:
    L.append(f"{r['ratio']}x (медиана {r['med']}) | {r['views']} просм | {r['likes']} лайков | отклик {r['reply']}% | охват {r['reach']}x | @{r['src']} | {r['at']} | тема={r['topic']}")
    L.append(f"   {r['url']} | {(r['title_ru'] or '')[:150]}")
L.append("")
L.append("### YouTube — абсолютные просмотры (топ-20)")
for r in sorted(y_out, key=lambda r: -r['views'])[:20]:
    L.append(f"{r['views']} просм | {r['likes']} лайков | отклик {r['reply']}% | ratio={r['ratio']} | @{r['src']} | {r['at']} | тема={r['topic']}")
    L.append(f"   {r['url']} | {(r['title_ru'] or '')[:150]}")
L.append("")
L.append("### Telegram (ИИ-каналы) — по кратности к норме канала")
for r in sorted([r for r in t_out if r['ratio'] and r['views'] >= 2000], key=lambda r: -r['ratio'])[:20]:
    L.append(f"{r['ratio']}x (норма {r['med']}) | {r['views']} просм | fwd {r['forwards']} | @{r['src']} | {r['at']}")
    L.append(f"   {r['url']} | {r['text'][:200]}")
open('/tmp/viral2.txt', 'w').write('\n'.join(L))
print('готово:', len(L), 'строк → /tmp/viral2.txt')
