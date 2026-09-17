# Tuber-x: расписание (ТЗ-4 Р6)

> **Документ описывает прежний контур.** Актуальные параметры (живой пул
> Nitter и частота/лимит скоринга) — в
> [`scores-cadence-20260917.md`](scores-cadence-20260917.md).

Задачи запускаются кроном на Main. Сеть — только через `tuber_x/channels.py`
(роутер каналов) и `tuber_x/nitter_broker.py` (транспорт).

| Задача | Частота | Команда | Что делает |
|--------|---------|---------|-----------|
| `collect` тир A | каждый час | `python3 -m tuber_x.cli collect --tier A` | сбор постов 20-постовой лентой + курсор |
| `collect` тир B | каждые 4 ч | `python3 -m tuber_x.cli collect --tier B` | то же для среднего тира |
| `collect` тир C | раз в сутки | `python3 -m tuber_x.cli collect --tier C` | низкопоточный хвост реестра |
| `enrich` | каждые 2 ч | `python3 -m tuber_x.cli enrich --batch 400` | догоняет метрики CDN пачками по 400 |
| `synd_snapshot` | **1 раз в сутки** | `python3 -m tuber_x.cli synd-snapshot --tier A --accounts 5` | разовое уточнение по репостам; канал разовый |
| `discover` | 4 раза в сутки | `python3 -m tuber_x.cli discover` | рост реестра (ТЗ-2) |
| `report` | 1 раз в сутки, утро | `python3 -m tuber_x.cli scores --limit 50` | выдача (ТЗ-3 отчёт + ТЗ-4 скоринг) |
| `health` | каждые 30 мин | `python3 -m tuber_x.cli health` | сторож: свежесть, отказы, бюджеты |

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
0 *  * * *  cd /root/tuber-x && python3 -m tuber_x.cli collect --tier A >> data/logs/collect-A.log 2>&1
15 */4 * * * cd /root/tuber-x && python3 -m tuber_x.cli collect --tier B >> data/logs/collect-B.log 2>&1
30 3  * * *  cd /root/tuber-x && python3 -m tuber_x.cli collect --tier C >> data/logs/collect-C.log 2>&1
40 4  * * *  cd /root/tuber-x && python3 -m tuber_x.cli synd-snapshot --tier A --accounts 5 >> data/logs/synd.log 2>&1
5 */2 * * *  cd /root/tuber-x && python3 -m tuber_x.cli enrich --batch 400 >> data/logs/enrich.log 2>&1
0 2,8,14,20 * * * cd /root/tuber-x && python3 -m tuber_x.cli discover >> data/logs/discover.log 2>&1
0 6  * * *  cd /root/tuber-x && python3 -m tuber_x.cli scores --limit 50 >> data/logs/scores.log 2>&1
*/30 * * * * cd /root/tuber-x && python3 -m tuber_x.cli health >> data/logs/health.log 2>&1
```
