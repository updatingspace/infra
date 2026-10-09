# UpdSpace: домашняя инфраструктура

Центральный локальный IaC-репозиторий для `updspace-home` (`192.168.1.176`).
Remote — `github.com/updatingspace/infra`, основная ветка `master`.
Все приложения размещаются в k3s; host systemd обслуживает сам k3s, диски и backup.

## Состав и границы применения

| Каталог | Управляемая конфигурация |
| --- | --- |
| `environments/home/host` | k3s, kubelet reservations, firewall, mount dependencies; Ansible |
| `environments/home/edge` | общий HTTP/TLS Caddy, маршруты, сеть, сохранённые volumes |
| `environments/home/dns` | семь Cloudflare CNAME: status, grafana, prometheus, alerts, errors, id, portal; proxy включён |
| `environments/home/observability-auth` | OIDC browser sessions; ID проверяет active + staff + system_admin |
| `environments/workstation` | согласованное локальное разрешение пяти monitoring-доменов через LAN |
| `environments/home/monitoring` | Grafana, Prometheus, Loki, Alertmanager, dashboard, datasource, 22 правила |
| `environments/home/glitchtip` | ошибки и трейсы через Sentry SDK, отдельная PostgreSQL DB, backup и restore probe |
| `environments/home/uptime-kuma` | приложение, PV, сеть, backup, проверки и публичная страница |
| `environments/pz/kubernetes` | перенесённый существующий Terraform PZ, collector, backup/updater и конфиги игры |
| `environments/home/pz-values` | несекретные параметры существующего домашнего размещения PZ |
| `environments/home/portal` | общий PostgreSQL, роли/схемы, backup и подготовленный Portal |
| `environments/home/id-platform` | ID, YDB, Garage, маршрутизация, конфиги и backup принятого пакета |
| `environments/home/teamspeak` | сохранённая MariaDB прежнего TeamSpeak в k3s; голосовой сервер не запускается |

ID/YDB/Garage приняты проверенным SHA-пакетом от их владельца; состояние
передачи записано в `docs/ownership.md`. Публичный ID переключён на k3s
2026-10-09; его production overlay включает приложения и пять CronJobs.
Portal также переключён на VM; итоговые manifests и acceptance находятся
в README его компонента. Старый PZ cloud source
включён как исходный IaC, а не как инструкция заново создать платные ресурсы.

Пароли, токены, TLS private keys, Terraform state, базы и backup не входят в Git.
Конфиги ссылаются на Kubernetes Secrets и закрытые операторские файлы. Для
восстановления нужны прежние секреты, данные и локально импортированные образы.
Один clone не заменяет их backup. Бюджеты CPU/RAM заданы workload manifests и
существующими ResourceQuota; Kubernetes capacity PV не является дисковой квотой.

## Общий HTTP-вход

Cloudflare → существующий Caddy в namespace `edge` → ClusterIP сервиса по Host.
Caddy остаётся единственным владельцем HTTP 80/HTTPS 443. Игровые UDP-порты
обслуживаются отдельными k3s Services. Proxy Cloudflare для объявленных доменов включён (`proxied: true`, TTL Auto); отключать его для ACME не нужно.

- https://status.updspace.com — публичная страница; `/dashboard` — админка Kuma.
- https://grafana.updspace.com
- https://prometheus.updspace.com
- https://alerts.updspace.com

Панели Grafana, Prometheus, Alertmanager, GlitchTip и админка Kuma защищены
через UpdSpace ID: **active И staff И system_admin**. Права читаются из ID на
каждом HTTP-запросе; открытые потоки принудительно переподключаются через минуту.
Basic auth снят; NodePort 30030/30031 закрыты. Публичная статусная страница
и приём SDK-событий GlitchTip доступны без staff-сессии. Контракт и проверки:
[ID access](docs/id-access.md).

Grafana, GlitchTip и Kuma сохраняют собственные учётные записи после входа через ID.
Grafana: Secret `observability/grafana-admin`, key `password`.
GlitchTip: https://errors.updspace.com, `admin@updspace.com`; пароль и DSN в
`/opt/updspace-infra/private/glitchtip-credentials.json` на VM, root 0600.
Приложение ограничено 1 CPU/1 GiB; PostgreSQL общий, его бюджет учитывается отдельно.
Инструкции применения и backup: [GlitchTip](environments/home/glitchtip/README.md).

## Проверка и ограниченное применение

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m unittest discover -s environments/home/monitoring -p 'test_*.py'
python3 -m unittest discover -s environments/home/portal -p 'test_*.py'
python3 -m unittest discover -s environments/pz/kubernetes/game-config -p 'test_*.py'
python3 environments/home/monitoring/build.py
python3 scripts/render.py > /tmp/updspace-access.json
sudo k3s kubectl apply --dry-run=server -f /tmp/updspace-access.json
sudo k3s kubectl diff -f /tmp/updspace-access.json
```

По умолчанию renderer выдаёт 11 объектов доступа: Caddy ConfigMap/Deployment,
три Deployment мониторинга, две закрытые ClusterIP Services и четыре NetworkPolicy. `--scope all` дополнительно
выдаёт snapshot edge/monitoring/Kuma для восстановления; он **не является**
монолитным deploy всей VM. Сначала отдельно установить `observability-auth` по его README. Остальные компоненты имеют свои инструменты и границы
в README. Не применять полный snapshot поверх кластера вслепую, не использовать
`--prune`. `kubectl diff` code 1 означает найденные различия.

Перед изменением общего edge сверить свежий live config и согласовать одного
writer. Проверить Caddyfile в закреплённом образе, сохранить предыдущие manifests,
затем применить только рассмотренный diff. У Caddy `admin off`, поэтому изменение
одного ConfigMap требует контролируемого restart deployment/caddy; на одном
узле будет короткое прерывание HTTPS. Игровой процесс перезапускать не требуется.
Старый Terraform state всё ещё содержит edge: ограничения в `docs/ownership.md`.

## Конфиги и проверка доступа

`uptime-kuma/config.cjs` по умолчанию проверяет drift, `--apply` синхронизирует
существующие проверки/настройки/статусную страницу. Сохраняются текущие паузы,
включая операторскую паузу spbetu.ru; Telegram пока не настроен.
PZ game-config требует остановленных game/panel и maintenance lock; сам ничего
не останавливает. PostgreSQL roles по умолчанию проверяются read-only, apply
транзакционный и сохраняет пароли. Детали находятся рядом с каждым инструментом.

`python3 scripts/verify-access.py` проверяет перенаправления всех пяти панелей
в ID, отказ подставным заголовкам и доступность публичной статусной страницы.
`--origin-ip 192.168.1.176` проверяет origin TLS с теми же hostname. Для проверки
полного Grafana frontend после своего входа можно передать закрытый Netscape
cookie-файл через `--cookie-file`; cookies и токены не печатаются.
Старые Basic credentials сохранены только для отката.

Cloudflare: `python3 scripts/dns.py` показывает drift, `--apply` согласует только
семь объявленных записей. Нужен внешний `CLOUDFLARE_API_TOKEN`; остальные записи
и настройки зоны не меняются. Storage DNS принадлежит передаче Garage отдельно.

## Восстановление и откат

На VM release copy расположен в `/opt/updspace-infra/source`, секреты отдельно
в `/opt/updspace-infra/private`. Перед доступом был сохранён
`before-monitoring-access.json`. Откат должен возвращать согласованные Caddyfile
и Deployment, не открывая мониторинг без аутентификации.

Сохранить `/srv/edge-caddy`, `/srv/pz-monitoring`, `/srv/uptime-kuma/data`, game/spool
loop files и новые data volumes. Retain PVC не заменяет backup. Существующие
импортированные image tags требуют OCI archives на новом узле. Полное
восстановление всей VM на чистом сервере этой задачей не проверено.

## CI и исторический Swarm

GitHub Actions запускает тесты Python/Node и Terraform с mock providers.
Слияние не применяет конфигурацию к VM: старый SSH workflow с `docker stack deploy`
и `docker image prune` отключён. Корневые `stack.yml` и `configs/` сохранены
как историческая конфигурация Swarm; текущий k3s описан в `environments/`.
Для полного локального набора проверок: `python3 scripts/validate.py`
(нужны age, zstd, rsync, ACL tools, Java и Node).
