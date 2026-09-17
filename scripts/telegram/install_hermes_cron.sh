#!/usr/bin/env bash
# Tuber-Telegram: установщик запускалок для планировщика Hermes (перенос
# scripts/install_hermes_cron.sh проекта tuber-telegram, ТЗ-4).
#
# Зачем это нужно. Планировщик Hermes умеет запускать скрипты ТОЛЬКО из каталога
# /root/.hermes/scripts/ и НЕ умеет передавать аргументы командной строки.
# Поэтому на каждое задание нужен отдельный файл-обёртка в этом каталоге.
#
# Почему НЕ символические ссылки. Планировщик перед запуском проверяет путь через
# tools/path_security.py::validate_within_dir, а тот вызывает Path.resolve() —
# то есть РАЗЫМЕНОВЫВАЕТ ссылку и требует, чтобы результат физически лежал внутри
# ~/.hermes/scripts/. Ссылка на /root/tuber/scripts/telegram/... этот тест не
# проходит. Поэтому установщик пишет НАСТОЯЩИЕ файлы-шимы: обычный файл,
# chmod +x, внутри — exec нужной обёртки проекта.
#
# Что делает:
#   * создаёт четыре настоящих файла-шима (tuber_telegram_collect.sh,
#     tuber_telegram_feed_export.sh, tuber_telegram_feed_import.sh,
#     tuber_telegram_discover.sh) только по известным именам;
#   * перед записью делает `rm -f` по конкретному имени: снимает прежний симлинк
#     (в том числе на что-то другое) или старый файл;
#   * идемпотентен: повторный запуск даёт те же шимы и код 0 (перезапись всегда);
#   * посторонние файлы в каталоге планировщика не трогает и не удаляет;
#   * печатает таблицу «шим | расписание | что делает» для регистрации владельцем.
#
# Задания в планировщике регистрирует ВЛАДЕЛЕЦ: установщик их не создаёт и в
# crontab не пишет. Блок JOBS ниже — единственный источник истины о расписании;
# его сверяет с планировщиком тест tests/telegram/test_schedule_sync.py.
#
# Режим проверки без записи: `--dry-run` или TUBER_TG_INSTALL_DRYRUN=1 — только
# печать, боевой каталог планировщика не трогается.
#
# Каталог можно подменить переменной HERMES_SCRIPTS_DIR (для тестов), корень
# проекта — TUBER_TG_PROJECT.
#
# Запуск: scripts/telegram/install_hermes_cron.sh [--dry-run]
set -uo pipefail

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
SCRIPTS_DIR="${HERMES_SCRIPTS_DIR:-/root/.hermes/scripts}"

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

DRYRUN="${TUBER_TG_INSTALL_DRYRUN:-0}"
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRYRUN=1
done

# Тройки «имя шима : обёртка проекта : аргумент». Пустой аргумент — exec без него.
LAUNCHERS=(
  "tuber_telegram_collect.sh:tuber_telegram_collect.sh:"
  "tuber_telegram_feed_export.sh:tuber_telegram_feed_export.sh:"
  "tuber_telegram_feed_import.sh:tuber_telegram_feed_import.sh:"
  "tuber_telegram_discover.sh:tuber_telegram_discover.sh:"
)

# Задания проекта: «имя шима : расписание (МСК) : подпись». Единственный источник
# истины; сюда входят и задания моста, и действующее задание сбора.
JOBS=(
  "tuber_telegram_feed_export.sh:30 6 * * *:Tuber-Telegram: экспорт фида X/YouTube"
  "tuber_telegram_feed_import.sh:35 6 * * *:Tuber-Telegram: импорт каналов из фида Tuber-OS"
  "tuber_telegram_discover.sh:40 7 * * *:Tuber-Telegram: дискавери каналов из своих постов"
  "tuber_telegram_collect.sh:*/30 * * * *:Tuber-Telegram: сбор постов (web)"
)

if [ "$DRYRUN" = "1" ]; then
  echo "Tuber-Telegram: ТЕСТОВЫЙ РЕЖИМ (боевой каталог $SCRIPTS_DIR не трогаем)"
else
  mkdir -p "$SCRIPTS_DIR" || {
    echo "ошибка: не удалось создать каталог $SCRIPTS_DIR" >&2
    exit 1
  }
  echo "Tuber-Telegram: установка запускалок в $SCRIPTS_DIR"
fi
echo "источник обёрток: $PROJECT/scripts/telegram"
echo ""
echo "установлено:"
shims=0
errors=0
for entry in "${LAUNCHERS[@]}"; do
  IFS=: read -r name wrapper arg <<<"$entry"
  target="$PROJECT/scripts/telegram/$wrapper"
  if [ ! -f "$target" ]; then
    echo "  ОШИБКА: нет обёртки $target" >&2
    errors=$((errors + 1))
    continue
  fi

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
    printf '# Tuber-Telegram: запускалка для планировщика Hermes. Создана install_hermes_cron.sh.\n'
    printf '# Правка вручную будет перезаписана при следующем запуске установщика.\n'
    printf 'if [ "${TUBER_TG_LAUNCHER_DRYRUN:-}" = "1" ]; then\n'
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
echo "задания для планировщика Hermes (время МСК), регистрирует владелец:"
for entry in "${JOBS[@]}"; do
  IFS=: read -r name schedule label <<<"$entry"
  printf '  %-31s %-11s %s\n' "$name" "$schedule" "$label"
done
echo ""
echo "в crontab ничего не записано: задания регистрирует владелец."
echo "обёртки печатают строку только при аномалии; в планировщике Hermes такие"
echo "строки доставляются владельцу в Telegram."

if [ "$errors" -ne 0 ]; then
  echo "ошибка: не установлено обёрток: $errors" >&2
  exit 1
fi
exit 0
