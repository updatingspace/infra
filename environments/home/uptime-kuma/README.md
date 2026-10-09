# Uptime Kuma на updspace-home

Сервер: `updspace_m4tveevm@192.168.1.176`. Kubernetes namespace: `uptime-kuma`.

- Локальная страница: http://192.168.1.176:30031/status/updspace
- Локальная панель: http://192.168.1.176:30031/dashboard
- Страница через общий Caddy и Cloudflare: https://status.updspace.com
- Панель через HTTPS: https://status.updspace.com/dashboard
- Администратор: `admin`. Пароль на сервере: `sudo cat /opt/uptime-kuma/admin-credentials.json` (0600, root).

Один контейнер `louislam/uptime-kuma:2.5.5-slim-rootless`, закреплённый по digest.
Requests: 50m CPU / 128Mi RAM; limits: 500m CPU / 512Mi RAM. SQLite размещена
в `/srv/uptime-kuma/data`, отдельный local PV с политикой Retain и PVC `data`.
Указанные 2Gi — размер Kubernetes volume, не квота файловой системы.
История хранится 90 дней. Пробы каждые 300 секунд, timeout 20 секунд,
две повторные попытки с интервалом 60 секунд, проверка сертификатов включена.

## Проверки

Исходный список: https://stats.uptimerobot.com/vpk7EdHoiY
Публичный API UptimeRobot скрывает точные URL; для сайтов использованы HTTPS-корни
доменов из имён проверок, поэтому полного совпадения с приватными настройками
UptimeRobot не заявляем. Старую историю не переносили.

`config.json` содержит текущее состояние: 8 сайтов, 4 локальных readiness endpoint и панель PZ.
`is-schedule.updspace.com`, `ttnr.ru`, `updspace.com` оставлены на паузе по указанию
пользователя. Проверка панели PZ включена после подтверждения нового DNS,
валидного HTTPS и ответа `/api/health` HTTP 200; первый heartbeat Kuma — UP.
Это не подтверждает доступность игрового UDP или вход игрока. Minecraft ещё не запущен,
поэтому проверка с предположительным портом не создавалась.
Приостановленные проверки остаются в админ-панели, в публичную сводку не включены.
Внутренние URL локальных сервисов не публикуются.

При установке `spbetu.ru` отвечает сертификатом `*.timeweb.ru`; HTTPS-проверка
корректно фиксирует отказ. При первоначальной установке остальные 8 активных
проверок ответили HTTP 200; затем успешно добавлена активная проверка панели PZ.

## Сеть

Общим HTTP/TLS reverse proxy владеет центральный infra: namespace `edge`,
pod label `app.kubernetes.io/name=caddy`, общий вход 80/443. Маршрут Kuma:
`status.updspace.com -> uptime-kuma.uptime-kuma.svc.cluster.local:3001`.
Игровые TCP/UDP-порты не направляются через HTTP-маршруты.

`access.yaml` разрешает доступ только из LAN `192.168.1.0/24` и общего Caddy.
NodePort 30031 использует externalTrafficPolicy Local, чтобы сохранить IP клиента.
`kuma.yaml` разрешает исходящие HTTP(S) в интернет, cluster DNS и четыре порта
локального мониторинга; дополнительных привилегий и service account token нет.
`health-from-uptime-kuma` — единственная добавленная policy в namespace observability.

Cloudflare: CNAME `status.updspace.com` к `updspacedd.tplinkdns.com`, proxied=true,
TTL Auto. Record ID: `5e56e98f0a16027d3db69682d60e4097`.
Для первоначального ACME временно использовалась A-запись домашнего IP:
DNS-only цепочка TP-Link DDNS возвращала NXDOMAIN на AAAA существующего имени.
После получения публично доверенного сертификата восстановлена CNAME с proxy.
В зоне включены Always Use HTTPS и TLS Full; общие настройки и другие DNS-записи
этой задачей не менялись. Origin TLS отдельно проверен с корректным SNI.
Сертификат и хранилище общего Caddy находятся в ведении общей edge-конфигурации.
Результаты первоначальной установки сохранены на VM в `/opt/uptime-kuma/acceptance.json`.

## Telegram

Пока отключён, токенов и notification objects нет. Когда будут бот и чат:
Settings -> Notifications -> Setup Notification -> Telegram. Заполнить поля
Bot Token и Chat ID, выполнить тест, затем привязать уведомление к проверкам.
Готовая форма встроена в Kuma, фиктивные токены не нужны.

## Эксплуатация

```sh
sudo k3s kubectl -n uptime-kuma get pods,pvc
sudo k3s kubectl -n uptime-kuma logs deployment/uptime-kuma --tail=60
sudo k3s kubectl -n uptime-kuma top pod
sudo systemctl start uptime-kuma-backup.service
sudo systemctl list-timers uptime-kuma-backup.timer
```

Бэкапы: `/srv/uptime-kuma/backups`, ежедневно в 04:20 UTC с задержкой до 10 минут,
последние 7 успешных снимков. Python SQLite backup API делает согласованный снимок
живой БД; `PRAGMA integrity_check` выполняется до его принятия. Копируются также
настройка БД и upload. Снимки содержат секреты и хранятся с правами root-only.
Это копии на том же диске: при его отказе они не обеспечивают восстановление.

Перед обновлением: выполнить backup service, проверить его успешное завершение,
затем изменить закреплённый image digest в `kuma.yaml` и применить манифест.
Deployment использует Recreate, чтобы два процесса не писали в одну SQLite.
Обновления образа автоматически не выполняются.

Для восстановления: остановить только deployment Kuma (`scale --replicas=0`),
сохранить текущий data каталог целиком для отката, восстановить содержимое выбранного
проверенного снимка в новый data каталог, выставить владельца 1000:1000 и режим 0700,
затем запустить deployment с одной репликой. Не смешивать старые WAL/SHM файлы
с восстановленной БД. PVC/PV и namespace для этого удалять не нужно.

Проверки при установке: server-side dry-run Kubernetes, фактические HTTP/TLS-пробы,
перезапуск pod, повторный вход администратора, запрет неавторизованного API,
закрытый bootstrap, сохранение пауз/истории, содержимое SQLite-снимка и браузерная
статусная страница. Дополнительно проверены публичные HTTPS, WebSocket, вход
администратора и запрет неавторизованного API через Cloudflare; внешний запрос
из облачной VM вернул HTTP 200 и правильный заголовок страницы. Полное
восстановление на отдельной машине не выполнялось.

## Конфигурация через IaC

`config.json` принят из текущей панели: 14 HTTP-проверок, настройки и группы
статусной страницы. Помимо трёх исходных пауз оператор приостановил spbetu.ru;
это состояние сохранено. Для обычного изменения сначала править этот JSON,
проверить diff, затем запустить `config.cjs --apply`. Прямое изменение в UI
нужно затем явно принять в Git, иначе check покажет drift.

`config.cjs` запускается внутри закреплённого Kuma container, где уже есть
Socket.IO client. JSON и script копируются в `/tmp/config.json`, `/tmp/config.cjs`;
учётные данные из `/opt/uptime-kuma/admin-credentials.json` подаются через stdin:

```sh
sudo cat /opt/uptime-kuma/admin-credentials.json | sudo k3s kubectl -n uptime-kuma exec -i deployment/uptime-kuma -- node /tmp/config.cjs /tmp/config.json
# Для явного применения добавить --apply к node-команде.
```

Check ничего не меняет, exit 1 означает drift/ошибку. Apply управляет только
заданными полями проверок, включая pause/resume; отсутствующие HTTP-проверки
создаёт без уведомлений. Имена должны быть уникальны. Не удаляет чужие
проверки, не сбрасывает пароли и не меняет привязки уведомлений. После восстановления
базы сначала вернуть прежний admin/monitor state из backup. Учётная запись администратора и статусная страница должны уже существовать;
это не bootstrap пустой БД. Telegram остаётся на будущее без фиктивных токенов.
