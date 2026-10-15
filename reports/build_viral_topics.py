#!/usr/bin/env python3
"""Выкладка «Виральные темы и посты Tuber» за окно (по образцу viral-topics-2026-09-25/26).

Источники: (1) /tmp/viral10.json — выгрузка extract_viral_topics2.py (is_ai=1, без
Маска/Альтмана); (2) штатный полный отчёт reports/report-<дата>.txt (блоки 1/4/5);
(3) агрегаты ярлыков и сквозные сюжеты — прямо из базы.
Ни одна ссылка не набирается вручную.

Режим пересборки без пересчёта данных:
    python3 reports/build_viral_topics.py --from-md <md> [--out-docx <docx>]
строит docx из готового markdown-файла той же логикой форматирования, что и
обычный прогон. Данные Tuber при этом не читаются, md не перезаписывается.
"""
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

def build_docx(lines, out_path):
    """Собрать docx из готового списка строк.

    Единая логика форматирования для обычного прогона и для режима --from-md:
    `# ` → 16pt bold, `## ` → 14pt bold, `N. ` → 11pt, отступ в 3 пробела →
    10pt, `- ` → `• ` 11pt, прочее → 11pt; шрифт Calibri. Пустые строки md
    пропускаются. Возвращает число абзацев (непустых строк).
    """
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    st = doc.styles['Normal']
    st.font.name = 'Calibri'
    st.font.size = Pt(11)

    def para(text, size=11, bold=False, space_after=4):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(space_after)
        r = p.add_run(text)
        r.font.size = Pt(size)
        r.bold = bold
        r.font.name = 'Calibri'

    for line in lines:
        if not line.strip():
            continue
        if line.startswith('# '):
            para(line[2:], size=16, bold=True, space_after=8)
        elif line.startswith('## '):
            para(line[3:], size=14, bold=True, space_after=6)
        elif re.match(r'^\d+\. ', line):
            para(line, size=11, space_after=2)
        elif line.startswith('   '):
            para(line.strip(), size=10, space_after=4)
        elif line.startswith('- '):
            para('• ' + line[2:], size=11, space_after=3)
        else:
            para(line, size=11, space_after=4)
    doc.save(out_path)
    return sum(1 for line in lines if line.strip())


def _parse_args(argv):
    """Разбор argv: --from-md <md>, --out-docx <docx>; прочее — дата и флаги."""
    from_md = out_docx = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--from-md':
            if i + 1 >= len(argv):
                sys.exit('--from-md требует путь к markdown-файлу')
            from_md = argv[i + 1]
            i += 2
        elif a.startswith('--from-md='):
            from_md = a.split('=', 1)[1]
            i += 1
        elif a == '--out-docx':
            if i + 1 >= len(argv):
                sys.exit('--out-docx требует путь к docx')
            out_docx = argv[i + 1]
            i += 2
        elif a.startswith('--out-docx='):
            out_docx = a.split('=', 1)[1]
            i += 1
        else:
            positional.append(a)
            i += 1
    return from_md, out_docx, positional


FROM_MD, OUT_DOCX_ARG, _POSITIONAL = _parse_args(sys.argv[1:])

DB = '/root/tuber/data/tuber.db'
_arg = next((a for a in _POSITIONAL if not a.startswith('--')), None)
DATE = os.environ.get('VIRAL_DATE') or _arg or datetime.now(timezone.utc).date().isoformat()

# Режим пересборки docx из готового markdown: данные Tuber не читаются, синтез тем
# не трогается, md не перезаписывается. Выходим до загрузки выгрузки и отчёта.
if FROM_MD:
    _md_lines = open(FROM_MD).read().splitlines()
    _out = OUT_DOCX_ARG or (os.path.splitext(FROM_MD)[0] + '.docx')
    _n = build_docx(_md_lines, _out)
    print('from-md:', FROM_MD)
    print('docx:', _out, '| абзацев:', _n)
    sys.exit(0)

V = json.load(open('/tmp/viral10.json'))
REP = open(f'/root/tuber/reports/report-{DATE}.txt').read().splitlines()
THEMES = f'/root/tuber/reports/viral-themes-{DATE}.md'
OUT_MD = f'/root/tuber/reports/viral-topics-{DATE}.md'
OUT_DX = f'/root/tuber/reports/viral-topics-{DATE}.docx'
MUSK = {'elonmusk', 'sama', 'openai', 'grok', 'xai', 'spacexai'}
SKIP_W = ('grok', 'musk', 'маск', 'altman', 'альтман')
URL_RE = re.compile(r'https?://\S+')


def n(x):
    return f'{x:,}'.replace(',', ' ') if isinstance(x, int) else x


def clean(text, limit=200):
    t = URL_RE.sub('', text or '')
    t = re.sub(r'\s+', ' ', t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    sp = cut.rfind(' ')
    return (cut[:sp] if sp > 0 else cut).rstrip(' ,;:—-') + '…'


def yt_title(r):
    """Заголовок видео: у русских каналов — русский, у остальных — оригинальный."""
    lang = (r.get('lang') or '')
    if lang.startswith('ru'):
        return r.get('title_ru') or r.get('title') or ''
    return r.get('title') or r.get('title_ru') or ''


def yt_note(r):
    """Строка-пояснение: чем видео на другом языке, для русского читателя — перевод заголовка."""
    lang = (r.get('lang') or 'не определён')
    if not (r.get('lang') or '').startswith('ru') and r.get('title_ru') \
            and r['title_ru'].strip() != (r.get('title') or '').strip():
        return f" ({lang}; перевод заголовка: {clean(r['title_ru'], 110)})"
    return f" ({lang})"


def x_has_ru(r):
    """Есть ли у X-поста русское описание из базы (classification)."""
    return bool((r.get('summary_ru') or '').strip() or (r.get('title_ru') or '').strip())


def x_desc(r):
    """Русское описание X-поста: summary_ru (до 300) → title_ru (до 220) → сырой текст.

    Перевод берётся только из базы (выгрузка extract_viral_topics2.py): в модель и в
    сеть не ходим. Последний фолбэк — оригинал, когда перевода нет.
    """
    if (r.get('summary_ru') or '').strip():
        return clean(r['summary_ru'], 300)
    if (r.get('title_ru') or '').strip():
        return clean(r['title_ru'], 220)
    return clean(r['text'])


def lang_line(rows):
    c = Counter((r.get('lang') or '') for r in rows)
    if set(c) <= {''}:
        return None  # язык в базе не размечен — строку не печатаем, чтобы не плодить «не определён»
    if '' in c:
        c['не размечен'] = c.pop('')
    return 'Языки в этом списке: ' + ', '.join(f'{k} — {v}' for k, v in c.most_common())


# ---------------- пулы из выгрузки ----------------
yt_all = [r for r in V['youtube'] if (r['src'] or '').lower() not in MUSK
          and not any(w in (r['title_ru'] or '').lower() for w in SKIP_W)]
x_all = [r for r in V['x'] if (r['handle'] or '').lower() not in MUSK
         and not any(w in (r['text'] or '').lower() for w in SKIP_W)]
tg_all = V['telegram']
lang_all = Counter((r.get('lang') or '') for r in yt_all)

yt_abs = sorted(yt_all, key=lambda r: -r['views'])[:15]
yt_rel = sorted([r for r in yt_all if r['ratio'] and r['ratio'] >= 20
                 and r['views'] >= 30000 and (r['reply'] or 0) >= 0.3],
                key=lambda r: -r['ratio'])[:12]
x_abs = sorted(x_all, key=lambda r: -r['likes'])[:12]
tg_fwd = sorted([r for r in tg_all if r['forwards']], key=lambda r: -r['forwards'])[:10]


def cap(rows, key, per_key=3, limit=None):
    """Не больше N позиций на один канал/автора — иначе список схлопывается в один канал."""
    seen, out = {}, []
    for r in rows:
        k = (key(r) or '').lower()
        if seen.get(k, 0) >= per_key:
            continue
        seen[k] = seen.get(k, 0) + 1
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


AI_TG = {h.lower() for h in V['ai_channels']}
x_abs = cap(sorted(x_all, key=lambda r: -r['likes']), lambda r: r['handle'],
            per_key=3, limit=12)
tg_fwd = cap(sorted([r for r in tg_all if r['forwards'] and r['src'].lower() in AI_TG],
                    key=lambda r: -r['forwards']), lambda r: r['src'], per_key=3, limit=10)

# ---------------- блоки штатного отчёта ----------------
yt_reach, tg_rel = [], []
for ln in REP:
    m = re.match(r'\s+(.+?) — канал (.+?); подписчиков ([\d\s]+), просмотров ([\d\s]+), '
                 r'в сутки (-?[\d\s]+), охват на подписчика ([\d.]+).*?опубликовано (\d+) дн\. назад; '
                 r'тема: (.+?), язык (\S+); (https://\S+)', ln)
    if m:
        yt_reach.append({'title': m.group(1).strip(), 'ch': m.group(2).strip(),
                         'subs': int(m.group(3).replace(' ', '')),
                         'views': int(m.group(4).replace(' ', '')),
                         'reach': float(m.group(6)), 'days': int(m.group(7)),
                         'topic': m.group(8), 'url': m.group(10)})
        continue
    m = re.match(r'\s+\[([\w_]+)\] (.+?) — просмотры ([\d\s]+), база канала ([\d\s]+), '
                 r'кратность ([\d.]+); опубликовано (\d+) дн\. назад.*? (https://\S+)', ln)
    if m:
        tg_rel.append({'ch': m.group(1), 'what': m.group(2).strip(),
                       'views': int(m.group(3).replace(' ', '')),
                       'base': int(m.group(4).replace(' ', '')), 'ratio': float(m.group(5)),
                       'days': int(m.group(6)), 'url': m.group(7)})

x_rel = cap(sorted([r for r in x_all if r['ratio'] and r['ratio'] >= 3
                    and (r['med'] or 0) >= 20 and (r['n_base'] or 0) >= 5],
                   key=lambda r: -r['ratio']),
            lambda r: r['handle'], per_key=2, limit=10)
# YouTube «вайр по охвату» считаем прямо по выгрузке (в ней есть язык и оригинальный заголовок),
# а не по тексту штатного отчёта: иначе в разделе нечего показать про язык и перевод названия.
yt_reach = cap(sorted([r for r in yt_all if r['reach']
                       and (r['subs'] or 0) >= 1000 and (r['views'] or 0) >= 100000],
                      key=lambda r: -r['reach']),
               lambda r: r['src'], per_key=2, limit=12)
# Telegram «выше нормы» считаем прямо по выгрузке (там уже только каналы с плотной ИИ-разметкой):
# в штатном отчёте этот блок не фильтруется по теме, поэтому в него попадают банки и сериалы.
tg_rel = cap(sorted([r for r in tg_all if r['ratio'] and r['ratio'] >= 1.5
                     and r['views'] >= 2000], key=lambda r: -r['ratio']),
             lambda r: r['src'], per_key=2, limit=10)

X_MISMATCH = sum(
    1 for r in x_all
    if (m := re.search(r'x\.com/([^/]+)/status', r['url'] or ''))
    and m.group(1).lower() != (r['handle'] or '').lower())

# ---------------- агрегат ярлыков и сюжеты из базы ----------------
con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
con.row_factory = sqlite3.Row
q = con.execute
START = V['start']
topics = q("""select cl.topic t, count(*) n,
                     sum(case when c.platform='youtube' then coalesce(cl2.views,0) else 0 end) v,
                     sum(case when c.platform='x' then coalesce(cl2.likes,0) else 0 end) l,
                     sum(cl2.forwards) f
              from content c join classification cl on cl.content_id=c.id and cl.is_ai=1
              left join content_latest cl2 on cl2.content_id=c.id
              where c.published_at>=? and cl.topic is not null and cl.topic<>''
              group by 1 having n>=50 order by n desc limit 15""", (START,)).fetchall()
topic_ex = {}
for r in topics:
    row = q("""select c.url, coalesce(cl.title_ru, c.title) hdr, cl2.views, cl2.likes
               from content c join classification cl on cl.content_id=c.id and cl.is_ai=1
               left join content_latest cl2 on cl2.content_id=c.id
               where c.published_at>=? and cl.topic=? and c.url is not null
               order by coalesce(cl2.views, cl2.likes, 0) desc limit 1""", (START, r['t'])).fetchone()
    if row:
        topic_ex[r['t']] = row

stories = []
cur = None
for ln in REP:
    m = re.match(r'\s+Сюжет (\d+) \[(.+?)\] — платформы: (.+)$', ln)
    if m:
        cur = {'id': m.group(1), 'title': m.group(2), 'plats': m.group(3).split(', '), 'links': []}
        stories.append(cur)
        continue
    m = re.match(r'\s+(\w+): (https://\S+)', ln)
    if m and cur is not None:
        cur['links'].append((m.group(1), m.group(2)))
NO_MUSK_S = ('musk', 'маск', 'spacex', 'starship', 'tesla', 'starlink')
stories = [s for s in stories
           if not any(w in s['title'].lower() for w in NO_MUSK_S)
           and not any('elonmusk' in u.lower() or 'spacex' in u.lower() for _, u in s['links'])]
stories.sort(key=lambda s: (-len(set(s['plats'])), -len(s['links'])))
stories_top = [s for s in stories if len(s['links']) >= 2][:10]

# ---------------- сборка markdown ----------------
L = []
def A(s):
    if s is not None:  # lang_line() возвращает None, когда языков в базе нет — такие строки пропускаем
        L.append(s)
A(f'# Виральные темы, посты и видео из Tuber — окно {V["window_days"]} суток')
A('')
A(f'Источник: боевая база Tuber (`{DB}`), окно {V["start"]} — {V["now"]} UTC, замеры '
  f'снапшотов — до последнего обхода.')
A('')
A('Что это простыми словами: за ' + str(V['window_days']) + ' суток из всей базы Tuber взято '
  'то, что помечено как ИИ-тема, и разложено по платформам — YouTube (видео), X (посты), '
  'Telegram (посты каналов). В каждом разделе — верхние материалы по своему показателю, '
  'с числами последнего замера и ссылкой на первоисточник.')
A('')
A('Откуда числа: это замеры самой базы Tuber (' + DB + ') — просмотры, лайки, пересылки, которые '
  'сборщик снял при последнем обходе. Живой перепроверки в момент выпуска нет: X у нас читается '
  'бесплатными каналами Tuber, платный X API идёт только на публикацию.')
A('')
A('Как читать показатели:')
A('- просмотры, лайки, пересылки — как в источнике на момент замера;')
A('- охват на подписчика — во сколько раз просмотров больше, чем подписчиков у канала; '
  'больше единицы значит, что видео посмотрели шире своей аудитории;')
A('- кратность к норме канала (у X — к норме автора) — во сколько раз материал выбился из '
  'своих обычных цифр; считается только там, где у канала набралось достаточно замеров, '
  'иначе стоит «норма канала: мало данных»;')
A('- отклик — доля лайков от просмотров.')
A('')
A('Про язык: заголовки видео не на русском даны в оригинале, строкой ниже — пометка языка и '
  'перевод заголовка из базы. Русскоязычные каналы здесь не большинство: в пуле окна ' +
  n(lang_all['en'] + lang_all['en-US'] + lang_all['en-GB'] + lang_all['en-IN']) +
  ' видео на английском, ' + n(lang_all['ru']) + ' на русском, дальше хинди, корейский, '
  'испанский, китайский. В начале каждого раздела первой строкой указано, сколько материалов '
  'на каком языке.')
A('')
A('Пул после фильтров: YouTube — ' + n(len(yt_all)) + ' видео, X — ' + n(len(x_all)) +
  ' постов, Telegram — ' + n(len(tg_all)) + ' постов из ' + n(len(V['ai_channels'])) +
  ' каналов с плотной ИИ-разметкой. Исключены посты Илона Маска, Сэма Альтмана и официальных '
  'аккаунтов OpenAI / xAI / Grok (выброшено ' + n(V['dropped_count']['x']) + ' постов X и ' +
  n(V['dropped_count']['youtube']) + ' видео).')
A('')
A('В списках X и Telegram — не больше 3 позиций на одного автора или канал '
  '(в разделах «выше нормы» — не больше 2), иначе верхние строчки занимает один аккаунт.')
A('')

A('## 1. YouTube: что реально посмотрели миллионы')
A('')
A(lang_line(yt_abs))
A('')
for i, r in enumerate(yt_abs, 1):
    extra = f", отклик {r['reply']}%" if r['reply'] else ''
    norm = f", кратность к норме канала {r['ratio']}x" if r['ratio'] else ', норма канала: мало данных'
    A(f"{i}. {yt_title(r)}")
    A(f"   канал {r['src']}{yt_note(r)}")
    A(f"   {n(r['views'])} просмотров, {n(r['likes'])} лайков{extra}{norm}")
    A(f"   {r['url']}")
A('')
A('## 2. YouTube: каналы, выстрелившие выше своей нормы')
A('')
A(lang_line(yt_rel))
A('')
for i, r in enumerate(yt_rel, 1):
    A(f"{i}. {yt_title(r)}")
    A(f"   канал {r['src']}{yt_note(r)}")
    med = n(r['med']) if r['med'] else 'нет данных'
    A(f"   {n(r['views'])} просмотров при обычных {med} — это {r['ratio']}x к норме "
      f"канала; лайков {n(r['likes'])}, отклик {r['reply']}%, охват {r['reach']}x от подписчиков")
    A(f"   {r['url']}")
A('')
A('## 3. X: максимум лайков и скорости за окно')
A('')
A(lang_line(x_abs))
A('')
for i, r in enumerate(x_abs, 1):
    A(f"{i}. @{r['handle']} — {x_desc(r)}")
    if x_has_ru(r):  # перевод есть — рядом показываем оригинал для сверки
        A(f"   оригинал: {clean(r['text'], 160)}")
    A(f"   {n(r['likes'])} лайков, {n(r['replies'])} ответов, {int(r['per_h'])} лайков/час, "
      f"тема: {r['topic']}")
    A(f"   {r['url']}")
A('')
A('## 4. X: посты, вылетевшие выше нормы своего автора')
A('')
for i, r in enumerate(x_rel[:10], 1):
    A(f"{i}. @{r['handle']} — {x_desc(r)}")
    if x_has_ru(r):  # перевод есть — рядом показываем оригинал для сверки
        A(f"   оригинал: {clean(r['text'], 160)}")
    A(f"   {n(r['likes'])} лайков, {n(r['replies'])} ответов, медиана автора {n(r['med'])} "
      f"по {n(r['n_base'])} постам, кратность {r['ratio']}x, лайков в час {r['per_h']}")
    A(f"   {r['url']}")
A('')
A('## 5. Telegram: что разошлось по пересылкам')
A('')
A(lang_line(tg_fwd))
A('')
for i, r in enumerate(tg_fwd, 1):
    A(f"{i}. @{r['src']} — {clean(r['text'])}")
    extra = f", кратность к норме канала {r['ratio']}x" if r['ratio'] else ''
    A(f"   {n(r['forwards'])} пересылок, {n(r['views'])} просмотров{extra}")
    A(f"   {r['url']}")
A('')
A('## 6. Telegram: выше нормы своего канала')
A('')
A(lang_line(tg_rel))
A('')
for i, r in enumerate(tg_rel[:10], 1):
    A(f"{i}. @{r['src']} — {clean(r['text'], 260)}")
    fwd = f", пересылок {n(r['forwards'])}" if r['forwards'] else ''
    A(f"   {n(r['views'])} просмотров при норме канала {n(r['med'])} — кратность {r['ratio']}x{fwd}")
    A(f"   {r['url']}")
A('')
A('## 7. YouTube: вайр по охвату на подписчика (маленький канал — крупный результат; '
  'в список берутся каналы от 1 000 подписчиков и видео от 100 000 просмотров, '
  'иначе показатель вырождается)')
A('')
A(lang_line(yt_reach))
A('')
for i, r in enumerate(yt_reach, 1):
    A(f"{i}. {yt_title(r)}")
    A(f"   канал {r['src']}{yt_note(r)}, подписчиков {n(r['subs'])}, просмотров {n(r['views'])} "
      f"— охват {r['reach']}x от подписчиков")
    A(f"   {r['url']}")
A('')
A('## 8. Темы окна: во что упирается поток')
A('')
A('Считалось по ярлыкам классификатора внутри окна — сколько материалов отнесено к теме '
  'на всех трёх платформах и какой у темы суммарный вес.')
A('')
for i, r in enumerate(topics, 1):
    ex = topic_ex.get(r['t'])
    parts = [f"просмотры YouTube {n(int(r['v']))}" if r['v'] else '',
             f"лайки X {n(int(r['l']))}" if r['l'] else '',
             f"пересылки Telegram {n(int(r['f']))}" if r['f'] else '']
    A(f"{i}. {r['t']} — {n(r['n'])} материалов; " + ', '.join(p for p in parts if p))
    if ex:
        A(f"   самый весомый материал темы: {clean(ex['hdr'], 150)} — {ex['url']}")
A('')
A('## 9. Сквозные сюжеты: одно событие на нескольких платформах')
A('')
for i, s in enumerate(stories_top, 1):
    A(f"{i}. Сюжет {s['id']} — {clean(s['title'], 180)}")
    A(f"   платформы: {', '.join(s['plats'])}; материалов в сюжете {len(s['links'])}")
    for plat, url in s['links'][:5]:
        A(f"   {plat}: {url}")
A('')
if len(sys.argv) > 1 and sys.argv[1] == '--themes':
    pass
try:
    themes = open(THEMES).read().strip()
except FileNotFoundError:
    themes = ''
if themes:
    A('## 10. Темы окна — синтез по всем платформам')
    A('')
    A(themes)
    A('')
A('## Оговорки')
A('')
A(f'- Числа — из замеров базы Tuber (последний обход снапшотов). Живая перепроверка через X API '
  f'не делалась: чтение X через api.x.com платное, решение владельца от 30.09.2026 — API только '
  f'для постинга.')
A('- Кратность к норме у X считается только там, где у автора есть ≥ 5 постов и медиана ≥ 20 '
  'лайков; без этой базы показатель вырождается, такие строки в документ не попали.')
A(f'- У {n(X_MISMATCH)} из {n(len(x_all))} постов X ссылка ведёт на пост из ленты-источника, '
  f'а автор текста — другой аккаунт: так устроен сбор, автор в строке указан реальный. '
  f'В разделе 3 это видно по расхождению имени автора и адреса ссылки.')
A('- Ярлыки тем в базе местами дублируют друг друга («модели и релизы» и «релизы моделей», '
  '«агенты и автоматизация» и «инструменты разработчика»), поэтому раздел 8 показывает их '
  'как есть — без склейки, чтобы не терять исходную разметку.')
A('- Охват на подписчика в YouTube считается только для каналов от 1 000 подписчиков с видео '
  'от 100 000 просмотров: у канала с 2–34 подписчиками любое видео даёт «тысячи иксов», это '
  'не результат, а дефект знаменателя; такие строки в раздел 7 не попали.')
A('- Подписчики X в базе не собираются, поэтому охват на подписчика для X невозможен — '
  'виральность X измеряется кратностью к норме автора.')
A('- Telegram: кратность печатается только там, где есть база нормы просмотров канала; '
  'где базы нет — только абсолютные просмотры и пересылки.')
A('- У постов X и Telegram поле языка в базе пустое, поэтому по языку эти платформы не делились; '
  'разбивка по языку печатается только для YouTube, где язык известен по метаданным видео.')
A('- Все ссылки взяты из базы Tuber и штатного отчёта, вручную не набирались. '
  f'Дата сборки: {datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")} UTC.')
md = '\n'.join(L)
open(OUT_MD, 'w').write(md + '\n')

# ---------------- docx ----------------
n_para = build_docx(L, OUT_DX)

print('md:', OUT_MD, len(md), 'знаков')
print('docx:', OUT_DX, '| абзацев:', n_para)
print('позиций: YT-абс', len(yt_abs), '| YT-норма', len(yt_rel), '| X-абс', len(x_abs),
      '| X-норма', len(x_rel), '| TG-fwd', len(tg_fwd), '| TG-норма', len(tg_rel),
      '| YT-охват', len(yt_reach), '| тем', len(topics), '| сюжетов', len(stories_top),
      '| синтез тем:', bool(themes))
