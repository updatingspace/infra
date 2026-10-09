# Владение инфраструктурой

Canonical Git: `/home/m4tveevm/PycharmProjects/infra`.
Release copy: `/opt/updspace-infra/source`.
Shared Caddy: `environments/home/edge/Caddyfile`. Старый
`/opt/pz-infrastructure/local-edge/Caddyfile` — compatibility copy, не редактировать
независимо. В центральном PZ source local-edge и local-monitoring используют
symlinks на общие canonical файлы. Общий edge меняет только один оператор за раз.

| Область | Источник после передачи | Проверенное состояние |
| --- | --- | --- |
| Edge, monitoring, Kuma, host | `environments/home` | Применено только изменение доступа; host принят без рестарта |
| PZ, collector, backup/updater | `environments/pz/kubernetes` | 157 исходников существующего working tree, затем явные центральные изменения |
| Параметры PZ и game config | `home/pz-values`, `pz/kubernetes/game-config` | Сняты с VM; game config check без изменения live |
| PostgreSQL, roles и Portal | `environments/home/portal` | Два SHA-verified пакета; PostgreSQL live, Portal подготовлен |
| ID, YDB, Garage | `environments/home/id-platform` | SHA-verified handoff; публичный ID остаётся в облаке, local ID — проба |
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

Пакет ID/HANDOFF.sha256 принят в `home/id-platform`, 28 файлов. Общий Caddy
содержит закрытый LAN stage ID и его TLS Secret mount; policy `caddy-to-id`
добавлена в edge snapshot. В полном приложении ID присутствует тот же policy
object: при объединённом рендере включать его один раз, не создавать второе владение.
Cloud ID не переключён, ID CronJobs приостановлены. Риск неподдерживаемой YDB
file-backed конфигурации и принятие production cutover относятся к задаче ID.
Stage certificate выдан до 2027-01-07 вручную; до этой даты или при cutover нужно
передать его обновление ACME. Monitoring host certificates обновляет общий Caddy.
