# Tuber-x: расписание (ТЗ-4 Р6)

> **Документ описывает прежний контур.** Актуальные параметры (живой пул
> Nitter и частота/лимит скоринга) — в
> [`scores-cadence-20260917.md`](scores-cadence-20260917.md).
>
> **Правка ТЗ-43B: команды ниже устарели.** Модуля `tuber_x.cli` в монорепо
> больше нет; вызов `python3 -m tuber.platforms.x collect --tier A` (пакет без
> `.__main__`) тоже не работает. Рабочие формы: `python3 -m tuber x collect
> --tier A` (обёртки расписания) либо `python3 -m tuber.platforms.x.cli collect
> --tier A`. Ниже `tuber_x.cli` заменён на `tuber.platforms.x.cli`, каталог
> `/root/tuber-x` — на `/root/tuber`.

Задачи запускаются кроном на Main. Сеть — только через `tuber_x/channels.py`
(роутер каналов) и `tuber_x/nitter_broker.py` (транспорт).

| Задача | Частота | Команда | Что делает |
|--------|---------|---------|-----------|
| `collect` тир A | каждый час | `python3 -m tuber.platforms.x.cli collect --tier A` | сбор постов 20-постовой лентой + курсор |
| `collect` тир B | каждые 4 ч | `python3 -m tuber.platforms.x.cli collect --tier B` | то же для среднего тира |
| `collect` тир C | раз в сутки | `python3 -m tuber.platforms.x.cli collect --tier C` | низкопоточный хвост реестра |
| `enrich` | каждые 2 ч | `python3 -m tuber.platforms.x.cli enrich --batch 400` | догоняет метрики CDN пачками по 400 |
| `synd_snapshot` | **1 раз в сутки** | `python3 -m tuber.platforms.x.cli synd-snapshot --tier A --accounts 5` | разовое уточнение по репостам; канал разовый |
| `discover` | 4 раза в сутки | `python3 -m tuber.platforms.x.cli discover` | рост реестра (ТЗ-2) |
| `followers` | **1 раз в сутки, 2:10 МСК** | `python3 -m tuber.platforms.x.cli followers --limit <остаток>` | снимки подписчиков на сессии X (ТЗ-44) |
| `report` | 1 раз в сутки, утро | `python3 -m tuber.platforms.x.cli scores --limit 50` | выдача (ТЗ-3 отчёт + ТЗ-4 скоринг) |
| `health` | каждые 30 мин | `python3 -m tuber.platforms.x.cli health` | сторож: свежесть, отказы, бюджеты |

## Задание `followers` (подписчики X, ТЗ-44)

Обёртка `scripts/x/tuber_x_followers.sh`, расписание — раз в сутки в **2:10 МСК**,
доставка `local`, `no_agent=True`. Регистрируется владельцем как
`Tuber-x: подписчики (сессия X)`.

Зачем: ряд роста подписчиков (ради которого заведён `source.subs` /
`source.subs_at` / `meta_json.followers_history`) сам не наполнялся — сбор был
возможен только вручную.

Чем ограничено — общим **бюджетом сессии X** (`X_SESSION_DAILY_CAP=800` за 24 ч
и окно `X_SESSION_WINDOW_CAP=50` за `X_SESSION_WINDOW_SEC=900` с, считается по
журналу `transport_request`, `kind='x_session'`). Обёртка:

* считает аккаунты-кандидаты (`status IN ('active','provisional','candidate')`)
  и берёт `--limit` = min(реестр, остаток суточного бюджета, потолок
  `TUBER_X_FOLLOWERS_LIMIT`), поэтому больше суточного бюджета за прогон не уйдёт;
* обходит реестр **порциями по остатку окна**: транспорт САМ поднимает
  `XSessionRateLimited` при исчерпании окна, поэтому один вызов CLI на 200+
  аккаунтов прервался бы на 50-м. Порция = остаток окна, между порциями обёртка
  ждёт освобождения окна; ротация в CLI (`subs IS NULL DESC, subs_at ASC`) сама
  подставляет следующие аккаунты;
* при норме молчит (журнал `/root/.hermes/logs/tuber_x_followers.log`), при
  отказе печатает одну строку `ALERT` по-русски.

Что делать при `429`: транспорт сам пишет cooldown в БД (`X_SESSION_429_COOLDOWN_SEC`
= 900 с, второй подряд `429` — `HARD` 3600 с). Обёртка на cooldown/blocked
останавливает обход и печатает `ALERT` (owner видит причину); повторный запуск в
тот же день вернётся к обходу сам, потому что цель считает **остаток** бюджета,
а журнал окна ведут все процессы. При `401/403` сессия помечается `blocked` на
сутки — обход пропускается, нужно обновить `config/x_session.json`.


## Почему `synd_snapshot` — разовый

Замер 15.09.2026 (probeF): после 15 минут ПОЛНОЙ тишины первый же запрос ленты
снова получил 429, вся серия 30/30 — 429. Окно восстановления больше 15 минут
(точная граница не найдена). Регулярный опрос этого канала запрещён: он стоит
как разовая уточняющая задача с бюджетом 5 запросов в сутки. Всё остальное
время распространение считается по графу упоминаний (`spread_src='graph'`).

## Владение инстансом Nitter (5-БИС, вариант «а»)

`config.INSTANCE_MODE='dedicated'`: Tuber-x ходит первым на свой инстанс
`nitter.jaydenha.uk`, чужой (`nitter.kareem.one`, боевой демон CryptoGraph)
оставлен аварийным fallback-ом. Строгий режим — `TUBER_X_INSTANCE_MODE=strict`.
Общий файловый семафор (вариант «б») реализован в `nitter_broker.SharedSemaphore`
и включается переменной `TUBER_X_NITTER_LOCK=/run/tuber-x-nitter.lock`; он нужен
при росте реестра выше 3000 аккаунтов.

## Пример crontab

```
# --- Tuber-x (ТЗ-1..ТЗ-4) ---
0 *  * * *  cd /root/tuber && python3 -m tuber.platforms.x.cli collect --tier A >> data/logs/collect-A.log 2>&1
15 */4 * * * cd /root/tuber && python3 -m tuber.platforms.x.cli collect --tier B >> data/logs/collect-B.log 2>&1
30 3  * * *  cd /root/tuber && python3 -m tuber.platforms.x.cli collect --tier C >> data/logs/collect-C.log 2>&1
40 4  * * *  cd /root/tuber && python3 -m tuber.platforms.x.cli synd-snapshot --tier A --accounts 5 >> data/logs/synd.log 2>&1
5 */2 * * *  cd /root/tuber && python3 -m tuber.platforms.x.cli enrich --batch 400 >> data/logs/enrich.log 2>&1
0 2,8,14,20 * * * cd /root/tuber && python3 -m tuber.platforms.x.cli discover >> data/logs/discover.log 2>&1
0 6  * * *  cd /root/tuber && python3 -m tuber.platforms.x.cli scores --limit 50 >> data/logs/scores.log 2>&1
*/30 * * * * cd /root/tuber && python3 -m tuber.platforms.x.cli health >> data/logs/health.log 2>&1
```
