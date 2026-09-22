# ТЗ-52. Оси «Сливок», чистота графа и зелёный прогон (D-66, D-57, D-53, D-65)

Дата: 21.09.2026. Проект: `/root/tuber`. Правки — только внутри проекта; боевая
база `data/tuber.db` не менялась (приёмка на копии).

## Что сделано

* **D-66.** X-путь заполняет `content_latest.views` тем же значением, что уже в
  снимке (`tuber/core/storage.py::recompute_content_latest` +
  `tuber/platforms/x/store.py::record_session_metrics`). Существующие снимки
  доводит `tuber metrics backfill` (`tuber/core/metrics.py::backfill_snapshots`).
  Оси «охват на подписчика» и «реакции на 1 000» теперь включают X, а выдача
  печатает базу КАЖДОЙ оси поимённо
  (`tuber/analysis/digest.py::_axis_base_line`, поле `axis_bases`).
* **D-57.** Введён `PAIR_MIN_SMALL_SIZE=1000` (взят из `slivki.SELECT_SUBS_MIN`,
  ТЗ-45) и признак `small_side_known`; пары с мелкой стороной ниже порога или с
  неизвестным размером в «кого читают верхние» не попадают
  (`tuber/core/firstmovers.py::pairs`, счётчик `pairs_skipped_min_size`).
* **D-53.** Тест `tests/x/test_tz6.py::test_report_translates_and_caches_in_report_texts`
  переписан на детерминированный вход (фиксированный якорь
  `stories.run(now=2026-09-15T23:00Z)` и фиксированный набор материалов), `xfail` снят.
* **D-65.** Обёртка `scripts/common/tuber_digest_slivki.sh` читает
  `TUBER_SLIVKI_ME` / `TUBER_SLIVKI_REACH|REACTIONS|G7` и передаёт их CLI; строка
  владельца вписана закомментированной с пометкой «подставить handle владельца
  или его числа». Без переменных блок честно печатает «НЕ найден в базе».
* Техдолг: D-53/D-57/D-65/D-66 → «закрыт» с числами; остаток — новый D-69
  (автопостановка канала владельца в реестр).

## Приёмка (числа)

1. `python3 -m pytest -q` → **1608 passed, 1 xfailed, 0 failed** (было: 1 failed,
   2 xfailed). Единственный `xfailed` — предсуществующий `tests/x/test_story_pairs.py` (D-31).
2. Копия боевой базы + `tuber metrics backfill`: строк X в `content_latest` с
   `views > 0` — **0 → 2 204** (все X-материалы, у которых снимок несёт просмотры;
   всего 10 133 X-снимков с `views>0` на 21.09.2026).
3. Рейтинг «Сливки» на копии: **10 авторов**. База каждой оси поимённо:
   * ось «охват на подписчика»: **684 YouTube + 581 Telegram + 87 X**;
   * ось «реакции на 1 000 просмотров»: **653 YouTube + 524 Telegram + 87 X**.
4. Граф: пар «крупный → мелкий» **20 → 18**, отсеяно новым порогом **2**
   (`subs=1` → ×103 065 и `subs=181` → ×569). Первым в списке идёт
   `elonmusk → natolambert` ×2377.68 (вырожденных ×100000 нет).
5. Живой пример перцентиля на копии: субъект `telegram:cbctvaz`
   (subs 12 754) → охват 0.0145, **перцентиль 1 из 100** (база 1 352 источника:
   684+581+87); реакции 10.87, перцентиль 48 из 100. Второй пример:
   `x:elonmusk` (subs 241 679 605) → охват 0.0024, перцентиль 1 из 100.
6. `TUBER_LAUNCHER_DRYRUN=1 bash scripts/common/tuber_digest_slivki.sh` → rc=0,
   печатает `/usr/bin/python3 -m tuber digest slivki --send --allow-production`;
   с `TUBER_SLIVKI_ME=telegram:cbctvaz` добавляется ` --me telegram:cbctvaz`.
   Без переменных — без `--me` (честное поведение).
7. `git show --stat` итогового коммита — см. ниже.

## Проверки

* Полный `pytest -q` — 0 failed, YouTube- и Telegram-пути не переписаны (их тесты зелёные).
* Значения владельца не выдуманы: в коде/тестах/отчёте нет подставленных subs/охвата/реакций.
