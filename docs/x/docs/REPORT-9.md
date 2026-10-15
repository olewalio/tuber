# ТЗ-9: запускалки для планировщика Hermes — шимы вместо симлинков

Дата: 15.09.2026. Исполнитель: jCode (`--local`). Репозиторий: `/root/tuber-x`.

## 1. Что было найдено (боевая установка)

Установщик ТЗ-7 создавал в `/root/.hermes/scripts/` десять **символических
ссылок** на обёртки проекта. Зарегистрировать по ним задание в планировщике
Hermes не удалось:

```
cronjob(script='tuber_x_collect_a.sh') →
"Script path escapes the scripts directory via traversal: 'tuber_x_collect_a.sh'"
```

Причина в коде планировщика:
`/usr/local/lib/hermes-agent/tools/cronjob_tools.py:553-562` вызывает
`tools/path_security.py::validate_within_dir`, а тот делает `Path.resolve()` —
**разыменовывает ссылку** и требует, чтобы результат физически лежал внутри
`~/.hermes/scripts/`. Замер на живом каталоге до правки:

```
tuber_x_collect_a.sh  симлинк=True  вердикт=Path escapes allowed directory:
    '/root/tuber-x/scripts/tuber_x_collect.sh' is not in the subpath of
    '/root/.hermes/scripts'
```

Вывод: симлинки планировщик Hermes не принимает никогда, установка ТЗ-7 в
нынешнем виде была нерабочей. Исправлено в ТЗ-9.

## 2. Что сделано

### Задача 1. Установщик пишет настоящие файлы-шимы

`scripts/install_hermes_cron.sh` переписан: вместо `ln -sfn` он делает `rm -f`
строго по конкретному имени и пишет обычный файл `cat > "$dest"` + `chmod +x`.
Содержимое шима (пример для тира B):

```bash
#!/usr/bin/env bash
# Tuber-x: запускалка для планировщика Hermes. Создана install_hermes_cron.sh.
# Правка вручную будет перезаписана при следующем запуске установщика.
if [ "${TUBER_X_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf "%s\n" "exec /root/tuber-x/scripts/tuber_x_collect.sh B"
  exit 0
fi
exec /root/tuber-x/scripts/tuber_x_collect.sh B
```

Для трёх тиров сбора тир передаётся **явным аргументом** (`A`/`B`/`C`): после
`exec` значение `$0` становится путём обёртки, суффикс `_a/_b/_c` теряется, и
определять тир из имени файла нечем. Остальные семь шимов делают `exec` без
аргументов. Режим `TUBER_X_LAUNCHER_DRYRUN=1` печатает ровно одну строку с
командой и выходит 0, ничего не запуская.

### Задача 2. Идемпотентность, уборка старого, чужие файлы

* Повторный запуск всегда перезаписывает шимы тем же содержимым и выходит 0.
* Прежние симлинки `tuber_x_*.sh` снимаются через `rm -f "$dest"` (строго по
  имени, в том числе если это ссылка на посторонний файл).
* Посторонние файлы `~/.hermes/scripts/` не трогаются: ни одного `rm -rf`, ни
  одной маски `rm` — только десять конкретных имён.
* В системный crontab ничего не пишется.
* Итоговая строка: `шимы установлены: 10 (симлинков: 0)`.

### Задача 3. Документация

* `docs/schedule.md`, раздел 7-б: описаны шимы, явная ссылка на
  `Path.resolve()` в `cronjob_tools.py`/`path_security.py`, передача тира
  аргументом и режим `TUBER_X_LAUNCHER_DRYRUN=1`.
* `docs/acceptance-log-9.txt`: журнал приёмки.
* Этот отчёт.

### Задача 4. Приёмка

`tools/acceptance_tz9.py` — 11 проверок на настоящих данных (см. журнал).
Все зелёные. Реальный сбор не запускался: топология проверяется dryrun-режимом.

## 3. Доказательства

### Шимы — обычные файлы (не ссылки)

```
-rwxr-xr-x 1 root root 438 Sep 15 15:13 /root/.hermes/scripts/tuber_x_collect_a.sh
-rwxr-xr-x 1 root root 438 Sep 15 15:13 /root/.hermes/scripts/tuber_x_collect_b.sh
-rwxr-xr-x 1 root root 438 Sep 15 15:13 /root/.hermes/scripts/tuber_x_collect_c.sh
-rwxr-xr-x 1 root root 432 Sep 15 15:13 /root/.hermes/scripts/tuber_x_enrich.sh
-rwxr-xr-x 1 root root 436 Sep 15 15:13 /root/.hermes/scripts/tuber_x_fulltext.sh
-rwxr-xr-x 1 root root 436 Sep 15 15:13 /root/.hermes/scripts/tuber_x_classify.sh
-rwxr-xr-x 1 root root 432 Sep 15 15:13 /root/.hermes/scripts/tuber_x_scores.sh
-rwxr-xr-x 1 root root 432 Sep 15 15:13 /root/.hermes/scripts/tuber_x_report.sh
-rwxr-xr-x 1 root root 428 Sep 15 15:13 /root/.hermes/scripts/tuber_x_synd.sh
-rwxr-xr-x 1 root root 432 Sep 15 15:13 /root/.hermes/scripts/tuber_x_health.sh
```

Симлинков среди `tuber_x_*`: **0**.

### dryrun тиров A, B, C

```
$ TUBER_X_LAUNCHER_DRYRUN=1 bash ~/.hermes/scripts/tuber_x_collect_a.sh
exec /root/tuber-x/scripts/tuber_x_collect.sh A        # rc=0
$ TUBER_X_LAUNCHER_DRYRUN=1 bash ~/.hermes/scripts/tuber_x_collect_b.sh
exec /root/tuber-x/scripts/tuber_x_collect.sh B        # rc=0
$ TUBER_X_LAUNCHER_DRYRUN=1 bash ~/.hermes/scripts/tuber_x_collect_c.sh
exec /root/tuber-x/scripts/tuber_x_collect.sh C        # rc=0
```

### Штатный механизм планировщика принимает все 10 путей

```
tuber_x_collect_a.sh     -> None
tuber_x_collect_b.sh     -> None
tuber_x_collect_c.sh     -> None
tuber_x_enrich.sh        -> None
tuber_x_fulltext.sh      -> None
tuber_x_classify.sh      -> None
tuber_x_scores.sh        -> None
tuber_x_report.sh        -> None
tuber_x_synd.sh          -> None
tuber_x_health.sh        -> None
```

(`None` = путь принят; до ТЗ-9 для `_a/_b/_c` здесь возвращалась строка
`Path escapes allowed directory: ...`.)

### Строка установщика

```
шимы установлены: 10 (симлинков: 0)
```

### Приёмка (11/11 OK)

```
[ 1] OK  среди 10 имён таблицы нет ни одного симлинка
[ 2] OK  все 10 файлов существуют, обычные, с правом на исполнение
[ 3] OK  содержимое каждого шима содержит ожидаемую команду exec (10/10)
[ 4] OK  dryrun для A/B/C печатает ровно ожидаемую строку, rc=0
[ 5] OK  validate_within_dir принимает каждый из 10 шимов (None)
[ 6] OK  файлы каталога вне списка 10 не изменены (153/153)
[ 8] OK  каталог reports/ не изменён (хэши совпали)
[10] OK  повторный запуск идемпотентен (содержимое и состав те же)
[11] OK  итоговая строка: «шимы установлены: 10 (симлинков: 0)»
[ 9] OK  полный набор тестов зелёный (222 passed)
[ 7] OK  рабочая БД не изменена (posts 484->484, снимки совпали)
```

## 4. Отличия от ТЗ-7

| | ТЗ-7 (было) | ТЗ-9 (стало) |
|---|---|---|
| Тип файла в `~/.hermes/scripts/` | симлинк | настоящий файл-шим |
| Принимает ли `validate_within_dir` | нет (escape) | да (None) |
| Тир для collect | из имени (суффикс `_a/_b/_c`) | явный аргумент `A`/`B`/`C` |
| Проверка без запуска | нет | `TUBER_X_LAUNCHER_DRYRUN=1` |
| Регистрация задания в Hermes | невозможна | возможна |

Имена десяти запускалок не изменились — заказчик регистрирует задания по ним же.

## 5. Изменённые файлы

* `scripts/install_hermes_cron.sh` — шимы вместо симлинков.
* `tests/test_tz7.py` — установщик проверяется как пишущий шимы (ТЗ-7 меняется ТЗ-9).
* `tests/test_tz9.py` — новый набор тестов ТЗ-9.
* `tools/acceptance_tz9.py` — приёмка ТЗ-9.
* `docs/schedule.md` — раздел 7-б.
* `docs/REPORT-9.md`, `docs/acceptance-log-9.txt`.

## 6. Ограничения и замечания

* Регистрацию заданий в планировщике Hermes выполняет заказчик (по десяти
  именам выше); в рамках ТЗ-9 она не делалась.
* Шим использует абсолютный путь `/root/tuber-x/scripts/...`; при переносе
  проекта установщик нужно запустить заново (путь берётся из `TUBER_X_PROJECT`).
* Боевая БД `data/tuber_x.db` и каталог `reports/` приёмкой не изменялись.
