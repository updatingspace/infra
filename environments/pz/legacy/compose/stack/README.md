# PZ: отдельные игра и панель

Рабочий каталог: `/opt/pz-stack`. Compose project: `pz-b42`.

- `zomboid`: только PZ и SteamCMD, образ `local/pz-server:runtime-1`, собственный процесс и политика `unless-stopped`. Не зависит от панели. Остановка контейнера сохраняет мир и отправляет `quit`; лимит ожидания — 5 минут.
- `panel`: веб-интерфейс `local/pz-panel:v1.3.7`, RCON `zomboid:27015`. Без Docker socket и контроллера all-in-one обновлений. Автозапуск игры и авторестарты модов отключены в существующей базе.
- `caddy`: TLS и проксирование на `panel:3001`.
- `otel-collector`: игра — `zomboid:9090`, проверка панели — `panel:3001`. Настройки Monium сохранены.

База панели и ключи находятся в `data/panel`, логи — `data/panel-logs`. Игра использует прежние `data/pz-server`, `data/zomboid`, `data/steam`. Панель читает игровые бинарные файлы через read-only mount; доступ к настройкам, логам, PanelBridge и резервным копиям сохранён через `/zomboid`.

## Обновление только панели

```sh
cd /opt/pz-stack
bin/update-panel.sh v1.3.7  # указать нужный релиз
```

Скрипт собирает только panel, запускает `up --no-deps --no-build --pull never --wait panel`, проверяет здоровье панели и неизменность ID, времени старта и счётчика перезапусков игры. Только после успеха атомарно сохраняет PANEL_REF в `.env`, сохраняя права доступа и остальные параметры. При ошибке проверки новая версия не записывается: изучить `docker compose logs panel`; повторить команду с прежней версией для отката образа. Перед обновлением на другой релиз сохранить резервную копию `data/panel/db.json`: если новый релиз меняет схему базы, для отката нужна база соответствующей версии. Ключи в `data/panel` сохраняются.

Простой перезапуск интерфейса:

```sh
docker compose restart panel
```

Управление игровым контейнером — отдельное действие оператора. RCON-команды в панели по-прежнему могут менять игровой мир; это интерфейс администратора, а не полностью read-only приложение. Кнопки локального запуска/обновления игры в панели для этой топологии не используются.

## Игра и обновления Steam

```sh
# Посмотреть игроков / сохранить мир:
docker compose exec -T zomboid python3 /usr/local/bin/pz-game rcon players
docker compose exec -T zomboid python3 /usr/local/bin/pz-game rcon save
# Штатно остановить или перезапустить:
docker compose stop zomboid
docker compose start zomboid
# либо: docker compose restart zomboid
```

Обновление файлов игры — только в согласованное окно, после остановки и резервного копирования. На обычном старте SteamCMD не запускается (старый PZ_UPDATE_ON_START более не управляет контейнером).

```sh
docker compose stop zomboid
docker compose run --rm --no-deps zomboid update-only
docker compose up -d --no-deps --no-build --pull never zomboid
```

Загрузка Workshop самим PZ при старте остаётся штатным поведением игры. Пересборка runtime выполняется отдельно: `docker compose build zomboid`, затем явное пересоздание только zomboid в окно обслуживания. Не использовать `down -v`, `rm -v` или удаление `data` для обновлений.

## Проверки

`tests/test_rcon.py` проверяет RCON framing, авторизацию и оборванные ответы. `tests/test_panel_update.py` проверяет, что обновление вызывает только build/up панели, не запускает отсутствующую игру и сохраняет остальные параметры и права `.env`. `tests/runtime-smoke.sh` запускает изолированный fake game: SIGTERM должен дать save/quit и exit 0; JVM flags сохраняются; update-only с занятым lock отклоняется.

`python3 tests/config-isolation.py` проверяет, что изменение PANEL_REF не меняет resolved configuration сервиса zomboid. `python3 bin/verify-panel-independence.py` пересоздаёт только panel, непрерывно опрашивая RCON; проверяет ID, StartedAt, RestartCount и PID игрового JVM. Отчёт сохраняется в `split-verification.json`. Проверку выполнять после полной загрузки мира.

## Откат первоначального разделения

Исходные Compose, Dockerfile, entrypoint, Caddyfile, `.env`, collector config и db.json сохраняются на сервере в `backups/split-<UTC timestamp>` (каталог 0700). Старый образ all-in-one сохранён.

Откат тоже требует остановки игры: сначала `docker compose stop panel zomboid`; восстановить файлы конфигурации и db.json из выбранного snapshot с сохранением владельца и режима; затем `docker compose up -d --no-deps --no-build --pull never zomboid caddy` и перезапустить collector с прежним config. Каталоги мира и ключи панели не заменять. Остановленный контейнер panel не запускать одновременно с all-in-one: они используют одну базу.

## Результат внедрения 19 сентября 2026

Стек разделён на сервере. После готовности мира панель была принудительно пересоздана с `--no-deps`: 11 запросов RCON прошли без ошибок. ID контейнера игры, время запуска, RestartCount и PID JVM остались прежними. Отчёт: `split-verification.json`. HTTPS панели и Prometheus игры отвечают HTTP 200; доставка метрик в Monium проверена.

Точка отката миграции: `/opt/pz-stack/backups/split-20260918T225811Z`.
