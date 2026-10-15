# HypeParser: веб-страница на сервере (nginx + systemd)

Пошаговая инструкция. Файлы в этом каталоге — **шаблоны**: их копирует
владелец, никакой установки автоматически не происходит.

Компоненты:

| Что | Откуда | Куда |
|-----|--------|------|
| Приложение (только stdlib) | `tuber/platforms/x/hype_web.py` | уже в репозитории |
| systemd-юнит | `deploy/tuber-hype.service` | `/etc/systemd/system/tuber-hype.service` |
| server-блок nginx | `deploy/nginx-hype.conf` | `/etc/nginx/sites-available/hype.conf` (+ симлинк в `sites-enabled`) |
| Зона `limit_req` | строка из шапки `nginx-hype.conf` | в `http { }` файла `/etc/nginx/nginx.conf` |

Приложение слушает `127.0.0.1:8730`; наружу отдаёт nginx на `/hype/`.
Существующий сайт `prop-rate` не трогаем: у нашего блока свой `server_name`
(`<server_host>`) и **нет** `default_server`.

---

## 1. Пароль для страницы (Basic Auth)

`auth_basic_user_file` в шаблоне — `/etc/nginx/.htpasswd_hype`. Создайте его.

Через `openssl` (есть везде, ничего ставить не надо):

```bash
# Логин: hype. Пароль спросит интерактивно (в истории не останется).
printf 'hype:' > /etc/nginx/.htpasswd_hype
openssl passwd -apr1 >> /etc/nginx/.htpasswd_hype
chmod 640 /etc/nginx/.htpasswd_hype
chown root:www-data /etc/nginx/.htpasswd_hype   # группа www-data у nginx (Ubuntu)
```

Либо через `htpasswd` (пакет `apache2-utils`), если он установлен:

```bash
htpasswd -c /etc/nginx/.htpasswd_hype hype
chmod 640 /etc/nginx/.htpasswd_hype
```

Проверка, что файл читается nginx: `sudo -u www-data cat /etc/nginx/.htpasswd_hype`.

---

## 2. systemd-юнит

```bash
cp /root/tuber/deploy/tuber-hype.service /etc/systemd/system/tuber-hype.service
systemctl daemon-reload
systemctl enable --now tuber-hype
systemctl status tuber-hype --no-pager
journalctl -u tuber-hype -n 50 --no-pager
```

`ExecStart` использует системный `/usr/bin/python3` из `WorkingDirectory=/root/tuber`.
Зависимостей нет — только стандартная библиотека, поэтому проектный venv
(`.venv-hype`) и боевое окружение не затрагиваются.

Локальная проверка (мимо nginx):

```bash
curl -sS http://127.0.0.1:8730/healthz
curl -sS "http://127.0.0.1:8730/search?q=AI%20lang%3Aen&pages=1" | head
```

---

## 3. Зона `limit_req` в `http{}` (без неё `nginx -t` упадёт)

Откройте `/etc/nginx/nginx.conf`, найдите блок `http { ... }` и добавьте **одну**
строку (например, рядом с `include /etc/nginx/conf.d/*.conf;`):

```nginx
limit_req_zone $binary_remote_addr zone=hype:10m rate=6r/m;
```

Почему именно здесь: `limit_req_zone` — директива уровня `http`/`server`, но не
`location`. Если оставить её в `sites-enabled/hype.conf` (это `server`-контекст,
он включается в `http`), строка тоже сработает; **в `location` — нет**. Поэтому
в шаблоне она вынесена отдельно и дана как комментарий-подсказка.

---

## 4. server-блок nginx

```bash
cp /root/tuber/deploy/nginx-hype.conf /etc/nginx/sites-available/hype.conf
# ВАЖНО: имя симлинка выбираем ТАК, чтобы оно сортировалось ПОСЛЕ `prop-rate`.
# nginx делает default-сервером первый server-блок для listen 80, а файлы из
# sites-enabled подключаются по алфавиту. `hype.conf` < `prop-rate` — наш блок
# стал бы default и перехватывал бы запросы с любым Host. `zz-hype.conf` >
# `prop-rate` — prop-rate остаётся default, как и было.
ln -sf /etc/nginx/sites-available/hype.conf /etc/nginx/sites-enabled/zz-hype.conf
nginx -t
```

Если `nginx -t` показывает `unknown limit_req_zone "hype"` — не добавлена строка
из шага 3. Если конфликт `duplicate default server` — убедитесь, что в шаблоне
**нет** слова `default_server` (его там быть не должно).

Перезагрузите nginx (без разрыва соединений):

```bash
systemctl reload nginx
```

Проверка, что prop-rate остался default-сервером и ничего не перехвачено:

```bash
# в выводе default-сервером для :80 должен быть prop-rate, а не hype
nginx -T | grep -nE 'server_name|default_server'
# запрос с «чужим» Host по-прежнему уходит на prop-rate, а не на наш 404
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Host: unknown.test' http://127.0.0.1/
```

---

## 5. Проверка снаружи

```bash
# 401 без пароля — значит Basic Auth работает:
curl -sS -o /dev/null -w '%{http_code}\n' http://<server_host>/hype/
# с паролем:
curl -sS -u hype:ПАРОЛЬ http://<server_host>/hype/healthz
curl -sS -u hype:ПАРОЛЬ "http://<server_host>/hype/search?q=AI%20lang%3Aen&pages=1" | head
# robots: запрет индексации
curl -sS -u hype:ПАРОЛЬ http://<server_host>/hype/robots.txt
# заголовок noindex
curl -sSI -u hype:ПАРОЛЬ http://<server_host>/hype/ | grep -i x-robots-tag
# чужой сайт prop-rate не сломан:
curl -sS -o /dev/null -w '%{http_code}\n' http://<server_host>/
```

---

## 6. Предохранители (уже в приложении)

* один поиск одновременно (замок в процессе), иначе `429` с русским текстом;
* пауза ≥ 10 с между стартами поисков;
* не более 5 страниц и 200 постов на запрос;
* суточный лимит поиска — штатный `X_SESSION_SEARCH_DAILY_CAP` (по умолчанию 100),
  при исчерпании показывается человеческий текст, а не стек;
* один поиск ≤ 180 с, отдаётся то, что успели получить;
* кэш запроса 10 минут — повторный тот же запрос не тратит бюджет X;
* ошибки транспорта — HTML с понятным русским текстом; трассировка только в
  `journalctl -u tuber-hype`, в браузер не уходит;
* по умолчанию страница **не пишет** в `x_search_post` (сухой путь). Запись
  включается флагом `--write` в `ExecStart` (только через функции `store`).

---

## 7. Обслуживание

```bash
systemctl restart tuber-hype       # перезапуск
journalctl -u tuber-hype -f        # живой журнал
tail -f /var/log/nginx/hype.access.log
systemctl disable --now tuber-hype # выключить
rm /etc/nginx/sites-enabled/zz-hype.conf && nginx -t && systemctl reload nginx
```
