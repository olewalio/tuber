#!/usr/bin/env bash
# Tuber-x: установщик запускалок для планировщика Hermes (ТЗ-7 задача 2, ТЗ-9
# задача 1, ТЗ-11 задача 2).
#
# Зачем это нужно. Планировщик Hermes умеет запускать скрипты ТОЛЬКО из каталога
# /root/.hermes/scripts/ и НЕ умеет передавать аргументы командной строки.
# Поэтому одной обёртки `tuber_x_collect.sh A` недостаточно: для каждого запуска
# нужен отдельный файл, который сам знает свой тир.
#
# Почему НЕ символические ссылки (ТЗ-9). Планировщик перед запуском проверяет
# путь через tools/path_security.py::validate_within_dir, а тот вызывает
# Path.resolve() — то есть РАЗЫМЕНОВЫВАЕТ ссылку и требует, чтобы результат
# физически лежал внутри ~/.hermes/scripts/. Ссылка на /root/tuber/scripts/x/...
# этот тест не проходит: задание по ней зарегистрировать нельзя. Поэтому
# установщик пишет НАСТОЯЩИЕ файлы-шимы: обычный файл, chmod +x, внутри — exec
# нужной обёртки. Для тиров A/B/C тир передаётся явным аргументом: после exec
# значение $0 становится путём обёртки, суффикс _a/_b/_c теряется, и определять
# тир из имени нечем.
#
# Что делает:
#   * создаёт (перезаписывает) ОДИННАДЦАТЬ настоящих файлов-шимов в каталоге
#     планировщика, только по известным именам (ТЗ-11 добавил дискавери);
#   * перед записью делает `rm -f` по конкретному имени: снимает прежний
#     симлинк (в том числе на что-то другое) или старый файл;
#   * идемпотентен: повторный запуск даёт те же шимы и код 0 (перезапись всегда);
#   * посторонние файлы в каталоге планировщика не трогает и не удаляет;
#   * печатает список установленного и полный список из 11 заданий для
#     планировщика с расписанием (МСК) и именем — чтобы заказчик мог их
#     зарегистрировать построчно.
#
# Режим проверки без запуска (для приёмки): если в окружении шима есть
# TUBER_X_LAUNCHER_DRYRUN=1, шим печатает в stdout одну строку с командой,
# которую исполнил бы, и выходит 0, ничего не запуская.
#
# ТЕСТОВЫЙ РЕЖИМ УСТАНОВЩИКА (ТЗ-11). Если задать TUBER_X_INSTALL_DRYRUN=1 или
# передать аргумент `--dry-run`, установщик только ПЕЧАТАЕТ, что будет сделано,
# и НЕ трогает боевой каталог планировщика. Это нужно приёмке: задания в
# боевом планировщике регистрирует заказчик, а не установщик (ТЗ-7).
#
# Чего НЕ делает (намеренно): не пишет ничего в crontab и не создаёт заданий.
# Установку заданий выполняет заказчик (см. docs/schedule.md).
#
# Каталог можно подменить переменной окружения HERMES_SCRIPTS_DIR (для тестов),
# корень проекта — TUBER_X_PROJECT.
#
# Запуск: scripts/install_hermes_cron.sh [--dry-run]
set -uo pipefail

PROJECT="${TUBER_X_PROJECT:-/root/tuber}"
SCRIPTS_DIR="${HERMES_SCRIPTS_DIR:-/root/.hermes/scripts}"

# Тестовый режим установщика: только печать, боевой каталог не трогаем.
DRYRUN="${TUBER_X_INSTALL_DRYRUN:-0}"
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRYRUN=1
done

# Тройки «имя шима : обёртка проекта : аргумент». Пустой аргумент — exec без него.
# Для трёх тиров сбора тир передаётся явно (см. шапку).
LAUNCHERS=(
  "tuber_x_collect_a.sh:tuber_x_collect.sh:A"
  "tuber_x_collect_b.sh:tuber_x_collect.sh:B"
  "tuber_x_collect_c.sh:tuber_x_collect.sh:C"
  "tuber_x_enrich.sh:tuber_x_enrich.sh:"
  "tuber_x_fulltext.sh:tuber_x_fulltext.sh:"
  "tuber_x_classify.sh:tuber_x_classify.sh:"
  "tuber_x_scores.sh:tuber_x_scores.sh:"
  "tuber_x_cross_stories.sh:tuber_x_cross_stories.sh:"
  "tuber_x_report.sh:tuber_x_report.sh:"
  "tuber_x_synd.sh:tuber_x_synd.sh:"
  "tuber_x_health.sh:tuber_x_health.sh:"
  "tuber_x_discover.sh:tuber_x_discover.sh:"
  "tuber_x_followers.sh:tuber_x_followers.sh:"
)

# Задания для планировщика: «имя шима : расписание (МСК) : имя задания».
# Расписание совпадает с docs/schedule.md; дискавери — 50 4 * * * (ТЗ-11).
# `tuber_x_scores.sh` — 40 */2 * * * (17.09.2026: сторож требует значимость в
# окне 6 ч, см. docs/x/docs/scores-cadence-20260917.md).
JOBS=(
  "tuber_x_collect_a.sh:0 * * * *:Tuber-x: сбор тира A"
  "tuber_x_collect_b.sh:15 */4 * * *:Tuber-x: сбор тира B"
  "tuber_x_collect_c.sh:30 3 * * *:Tuber-x: сбор тира C"
  "tuber_x_enrich.sh:5 * * * *:Tuber-x: метрики вовлечённости (enrich, каждый час)"
  "tuber_x_fulltext.sh:20 * * * *:Tuber-x: полный текст (fulltext)"
  "tuber_x_synd.sh:40 4 * * *:Tuber-x: снимок ленты (synd)"
  "tuber_x_discover.sh:50 4 * * *:Tuber-x: расширение реестра (дискавери)"
  "tuber_x_classify.sh:20 */2 * * *:Tuber-x: классификация (classify)"
  "tuber_x_scores.sh:40 */2 * * *:Tuber-x: сюжеты и оценки (scores)"
  "tuber_x_cross_stories.sh:50 6 * * *:Tuber: сквозные сюжеты (X+Telegram+YouTube)"
  "tuber_x_report.sh:0 7 * * *:Tuber-x: ежедневный отчёт (report)"
  "tuber_x_followers.sh:10 2 * * *:Tuber-x: подписчики (сессия X)"
  "tuber_x_health.sh:*/30 * * * *:Tuber-x: сторож (health)"
)

if [ "$DRYRUN" = "1" ]; then
  echo "Tuber-x: ТЕСТОВЫЙ РЕЖИМ (боевой каталог $SCRIPTS_DIR не трогаем)"
else
  mkdir -p "$SCRIPTS_DIR" || {
    echo "ошибка: не удалось создать каталог $SCRIPTS_DIR" >&2
    exit 1
  }
  echo "Tuber-x: установка запускалок в $SCRIPTS_DIR"
fi
echo "источник обёрток: $PROJECT/scripts/x"
echo ""
echo "установлено:"
shims=0
errors=0
for entry in "${LAUNCHERS[@]}"; do
  IFS=: read -r name wrapper arg <<<"$entry"
  target="$PROJECT/scripts/x/$wrapper"
  if [ ! -f "$target" ]; then
    echo "  ОШИБКА: нет обёртки $target" >&2
    errors=$((errors + 1))
    continue
  fi

  # Команда, которую шим исполнит (и которую печатает dryrun).
  cmd="exec $target"
  if [ -n "$arg" ]; then
    cmd="$cmd $arg"
  fi

  if [ "$DRYRUN" = "1" ]; then
    echo "  будет создан файл $SCRIPTS_DIR/$name -> $cmd"
    shims=$((shims + 1))
    continue
  fi

  dest="$SCRIPTS_DIR/$name"
  # Снимаем прежний симлинк/файл строго по этому имени (без масок и rm -rf).
  rm -f "$dest"
  # Настоящий файл-шим: обычный файл, не ссылка.
  {
    printf '#!/usr/bin/env bash\n'
    printf '# Tuber-x: запускалка для планировщика Hermes. Создана install_hermes_cron.sh.\n'
    printf '# Правка вручную будет перезаписана при следующем запуске установщика.\n'
    printf 'if [ "${TUBER_X_LAUNCHER_DRYRUN:-}" = "1" ]; then\n'
    printf '  printf "%%s\\n" "%s"\n' "$cmd"
    printf '  exit 0\n'
    printf 'fi\n'
    printf '%s\n' "$cmd"
  } > "$dest"
  chmod +x "$dest"
  if [ ! -f "$dest" ]; then
    echo "  ОШИБКА: не удалось записать $dest" >&2
    errors=$((errors + 1))
    continue
  fi
  echo "  $name -> $cmd"
  shims=$((shims + 1))
done

if [ "$DRYRUN" = "1" ]; then
  echo ""
  echo "итого запускалок: ${#LAUNCHERS[@]}"
  echo "в тестовом режиме ничего не записано (боевой каталог не тронут)"
else
  # Сколько из наших имён осталось симлинками (должно быть 0).
  symlinks=0
  for entry in "${LAUNCHERS[@]}"; do
    IFS=: read -r name _ _ <<<"$entry"
    if [ -L "$SCRIPTS_DIR/$name" ]; then
      symlinks=$((symlinks + 1))
    fi
  done
  echo ""
  echo "итого запускалок: ${#LAUNCHERS[@]}"
  echo "шимы установлены: $shims (симлинков: $symlinks)"
fi

echo ""
echo "задания для планировщика Hermes (время МСК), регистрирует заказчик:"
for entry in "${JOBS[@]}"; do
  IFS=: read -r name schedule label <<<"$entry"
  printf '  %-11s %-24s %s\n' "$schedule" "$name" "$label"
done
echo ""
echo "в crontab ничего не записано: задания регистрирует заказчик."
echo "сторож tuber_x_health.sh печатает только строки ALERT; в планировщике"
echo "Hermes такие строки доставляются владельцу в Telegram."
echo "в системный crontab сторож ставить нельзя: почту он не доставляет."

if [ "$errors" -ne 0 ]; then
  echo "ошибка: не установлено обёрток: $errors" >&2
  exit 1
fi
exit 0
