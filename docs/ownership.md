# Владение инфраструктурой

Canonical Git: `/home/m4tveevm/PycharmProjects/infra`.
Release copy: `/opt/updspace-infra/source`.
Shared Caddy: `environments/home/edge/Caddyfile`. Старый
`/opt/pz-infrastructure/local-edge/Caddyfile` — compatibility copy, не редактировать
независимо. В центральном PZ source local-edge и local-monitoring используют
symlinks на общие canonical файлы. Общий edge меняет только один оператор за раз.

| Область | Источник после передачи | Проверенное состояние |
| --- | --- | --- |
| Edge, monitoring, Kuma, GlitchTip, host | `environments/home` | Приложения live; GlitchTip errors/spans и offsite restore проверены; host принят без рестарта |
| PZ, collector, backup/updater | `environments/pz/kubernetes` | 157 исходников существующего working tree, затем явные центральные изменения |
| Параметры PZ и game config | `home/pz-values`, `pz/kubernetes/game-config` | Сняты с VM; game config check без изменения live |
| PostgreSQL, roles и Portal | `environments/home/portal` | Публичные frontend/API на VM; 78 таблиц / 371 строка и 2 media objects восстановлены и проверены |
| ID, YDB, Garage | `environments/home/id-platform` | Публичный ID на k3s с 2026-10-09; final snapshot, media, jobs, backup и public acceptance проверены |
| Legacy TeamSpeak DB | `environments/home/teamspeak` | Состояние переноса в README компонента |

## Terraform и snapshot

Исторический Terraform PZ сохраняет state вне Git. Нельзя запускать старый
workloads apply без актуальных home values: это может вернуть прежний Caddyfile,
убрать Secret refs/mounts и сменить edge storage path. Центральные values содержат
`caddy_config_path`, `edge_storage_root`, `monitoring_auth_secret_name` и
`id_origin_tls_secret_name`. Центральный Terraform сохраняет оба Secret reference;
исторические checkout/values этого не делают. До сверки полного live плана
edge/resources.json — текущий recovery snapshot;
он не является независимым конкурентным deploy для Terraform.

Перед автоматизацией единого deploy проверить план центрального Terraform с
актуальными home values и существующим state. При выделении edge в отдельный
state передавать bindings без удаления ресурсов.
Не использовать destroy/recreate для передачи владения. Live state migration
в этой задаче не выполнялась. Мониторинг и приложения управляются компонентно,
а не массовым apply всех snapshots. Kubernetes coreDNS/metrics-server/ServiceLB
создаются самим k3s; их generated manifests не поддерживаются вручную.

`docs/provenance` хранит SHA исходных handoff-пакетов. После центральных изменений
и symlinks исходные SHA не описывают весь текущий tree; история Git фиксирует
разницу. Первоначальные репозитории и их uncommitted changes не изменялись нами.
По уточнению владельца центральный IaC публикуется в существующий
`github.com/updatingspace/infra` через PR в `master`, поверх его истории.
Отдельный `github.com/updspace/infra` не создаётся.

## Передача ID

Исходный SHA-пакет и дополнения сохранены в `home/id-platform`. Публичный ID
переключён на k3s 2026-10-09; production overlay включает приложения и пять
CronJobs. Финальный снимок содержит 67 таблиц, 1833 строки, 19 пользователей и
6 media objects; acceptance и проверки восстановления записаны в JSON компонента.
Облачные данные сохранены, запись из прежних процессов ограждена IAM.

ID использует автоматический ACME общего Caddy и передаёт реальный IP только
из доверенных диапазонов Cloudflare. `caddy-to-id` присутствует также в manifests
ID: при объединённом рендере включать объект один раз. Риск file-backed YDB принят
оператором в задаче ID и не отменяется успешным переносом. Интеграция доступа к
мониторингу через ID staff AND network administrator ещё не включена.

Итоговый Caddyfile принят после публичной проверки Portal: SHA256
`a42c0259a66fc35e3f10c43b4b0fff4e4e9d16e73bee8a79a53873da22c097dc`.
ID и Portal используют локальные API. Сертификат Portal пока ручной LE,
его срок и процедуру продления отслеживать по README компонента.

## Сокращение облака и workstation backups, 2026-10-09

После принятого переноса ID/Portal их старый cloud runtime удалён отдельным
allowlist-планом. YDB/S3/registry/Lockbox и ресурсы других проектов сохранены.
Пользователь отменил хранение ID/Portal backup на рабочем компьютере: два
offsite timers остановлены, архивы удалены, закрытый GPG key сохранён.
VM backup timers продолжают работать. Детали и границы восстановления —
[cloud-retirement-2026-10-09.md](cloud-retirement-2026-10-09.md).
