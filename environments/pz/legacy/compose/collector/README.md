> Актуальная структура IaC и инструкция: [infrastructure/README.md](../../README.md). Этот документ сохраняет историю первоначальной настройки; определения действующих дашбордов — в `../dashboards`, алерты — в `../../terraform/alerts.tf.json`.

# Project Zomboid → YC Monium

Установлено на `matveevmihail@51.250.40.253`, Compose-проект `pz-b42`, каталог `/opt/pz-stack`.

Collector 0.161.0 закреплён по digest. Он запускается отдельным сервисом `otel-collector` в основном `docker-compose.yml`. Файл `compose.service.yaml` содержит фрагмент этого сервиса для справки; отдельно запускать его не нужно.

## Данные в Monium

- Текущий проект **приёма метрик**: `folder__b1gidr45ifb2c25bco2d`.
- Создан отдельный проект **дашбордов** `pz-zomboid`, ID `mond4r1ecj79b4qh7g11`. Приём телеметрии в нём пока не активирован: пользователь должен привязать существующий платёжный аккаунт. До этого Collector продолжает писать в прежний проект, а новые дашборды читают его данные.
- Кластер: `pz-b42`.
- `service=zomboid`: Prometheus-метрики игры, Storm и JVM; сбор каждые 30 секунд.
- `service=pz-host`: CPU, память, paging, диск `vda`, заполнение корневой файловой системы; каждые 60 секунд.
- `service=pz-docker`: ресурсы, перезапуски и uptime контейнеров; каждые 60 секунд.
- `service=pz-panel`: HTTP-проверка `http://panel:3001/api/health`; каждые 30 секунд. Это проверка панели, а не готовности игрового мира.
- `service=otel-collector`: метрики работы и доставки самого Collector; каждые 60 секунд.

Отправка: OTLP/HTTP protobuf, TLS, zstd, `https://ingest.monium.yandex.cloud/otlp/v1/metrics`. Монотонные счётчики сохраняются кумулятивно; скорость изменений рассчитывается в запросах к графикам.

Открыть [Monium](https://monium.yandex.cloud/), выбрать проект, затем «Исследование → Метрики» и кластер/сервис. Пример селектора для игровой цели:

```text
{project="folder__b1gidr45ifb2c25bco2d", cluster="pz-b42", service="zomboid", name="up"}
```

## Дашборды

| Дашборд | Содержание |
| --- | --- |
| [01 · PZ — состояние сервера](https://monium.yandex.cloud/projects/mond4r1ecj79b4qh7g11/dashboards/fbebhclihlcl4dtaer7v?from=now-1h&to=now&refresh=60000) | 9 виджетов: доступность, игроки, свободный диск, TPS и цель, средний тик, CPU, заполнение диска, heap, очередь доставки |
| [02 · PZ — игра и JVM](https://monium.yandex.cloud/projects/mond4r1ecj79b4qh7g11/dashboards/fbeft68e6ut7sqmf13v4?from=now-1h&to=now&refresh=60000) | 9 виджетов: TPS, тик, игроки, CPU процесса, heap, RSS, потоки/deadlocks, GC, объекты мира |
| [03 · PZ — хост, Docker и доставка](https://monium.yandex.cloud/projects/mond4r1ecj79b4qh7g11/dashboards/fbe72vtm52fkrcjduunu?from=now-1h&to=now&refresh=60000) | 12 виджетов: CPU, load, память, диск, I/O, Docker, перезапуски и доставка |

Форматы: KPI-плитки для текущих значений, временные графики для динамики, встроенная «Сводная таблица → Копировать CSV» для выгрузки значений. Настройки воспроизводятся из `dashboards/*.json` через настройки дашборда → JSON → применить → сохранить. Генератор использует только стандартную библиотеку Python:

```sh
python monitoring/build-dashboards.py
```

Счётчики преобразуются через `non_negative_derivative`, отрицательные скачки при сбросе не отображаются как отрицательная нагрузка. TPS сравнивается с `storm_server_lock_fps`. Средний тик рассчитан по скоростям `.sum` и `.count`; p95/p99 не строятся, поскольку текущий экспортёр отдаёт только bucket `+Inf`. CPU процесса/контейнера: 100% = одно ядро; CPU хоста нормирован на число логических CPU. Память отображается в GiB, длительность тика — в ms, I/O — в B/s и IOPS. Состояния памяти хоста не складываются. ZGC Cycles и Pauses показаны отдельно; время concurrent cycle не равно stop-the-world паузе.

Плитки показывают последнее доступное значение за выбранный интервал; их цвет не является алертом и не доказывает свежесть данных. Пропуски в графиках не заменяются нулём. `up` проверяет экспортёр, а HTTP health — панель; это не проверка входа игрока.

Алерты и каналы уведомлений пока не создавались. Логи и трейсы отложены по запросу пользователя.

### Завершение переноса приёма в pz-zomboid

1. В [настройках проекта](https://monium.yandex.cloud/projects/mond4r1ecj79b4qh7g11/settings) привязать существующий платёжный аккаунт. Этот шаг ожидает пользователя.
2. Роль `monium.metrics.writer` аккаунту `pz-monium-writer` уже выдана и проверена в новом проекте. Новый API-ключ не нужен.
3. В локальном и серверном `otel-collector.yaml` поменять только `x-monium-project` на `mond4r1ecj79b4qh7g11`. Проверить конфигурацию, перезапустить только `otel-collector` командами ниже. Проверить рост отправленных точек и отсутствие ошибок авторизации/биллинга.
4. Выполнить `python monitoring/build-dashboards.py --project mond4r1ecj79b4qh7g11`, применить JSON к трём существующим дашбордам и проверить новые точки. Зафиксировать новый проект в этом README и deployment.json.

История старого проекта не переносится автоматически и остаётся доступной в нём. До завершения этих шагов не переключать запросы на пустой новый проект.

## Доступ

Сервисный аккаунт `pz-monium-writer`, ID `aje25u06h4nadtlvfrsj`, роль `monium.metrics.writer` в каталоге `b1gidr45ifb2c25bco2d` и в отдельном проекте `mond4r1ecj79b4qh7g11`.

API-ключ `ajeg37pesu32ocpr511b` имеет scope `yc.monium.metrics.write`. Истекает **2027-09-18 00:00 UTC**. До этой даты ключ необходимо заменить. Секрет находится только на сервере: `/opt/pz-stack/monitoring/monium.env`, права `600`; метаданные ключа — `monium-key-info.json`. Не копировать эти файлы в Git и не публиковать вывод `docker compose config` или `docker inspect`, содержащий переменные окружения.

Collector читает файловую систему хоста и Docker socket для сбора системных метрик. Порты диагностики `8888` и `13133` опубликованы только на loopback сервера. Лимиты: 256 MiB RAM, 0.5 CPU, Docker-логи 3 × 10 MB. Очередь отправки ограничена и хранится в памяти; это мониторинг, не архив телеметрии.

## Проверка и обслуживание

Выполнять на сервере:

```sh
cd /opt/pz-stack
docker compose ps
curl -fsS http://127.0.0.1:13133/
curl -fsS http://127.0.0.1:8888/metrics | grep -E '^otelcol_(exporter_(sent_metric_points|send_failed_metric_points|queue_size)|scraper_errored_metric_points)'
docker compose logs --tail 50 otel-collector
```

Рост `otelcol_exporter_sent_metric_points`, нулевая очередь и отсутствие ошибок подтверждают доставку. После создания нового service/cluster Monium может некоторое время возвращать `Location of shard ... is not known`; Collector повторяет запросы.

После изменения `monitoring/otel-collector.yaml`:

```sh
docker compose exec otel-collector /otelcol-contrib validate --config=/etc/otelcol/config.yaml
docker compose restart --no-deps otel-collector
```

После изменения ключа или параметров сервиса:

```sh
docker compose up -d --no-deps --no-build --pull never --force-recreate otel-collector
```

Эти команды затрагивают только Collector. Остановить отправку: `docker compose stop otel-collector`. Для этого не нужны `compose down`, очистка Docker или перезапуск игры.

## Документация

- [OTLP в Monium](https://yandex.cloud/en/docs/monium/collector/otlp-protocol)
- [Просмотр метрик](https://yandex.cloud/ru/docs/monium/metrics/metric-explorer)
- [Настройка Collector](https://yandex.cloud/ru/docs/monium/collector/opentelemetry)
