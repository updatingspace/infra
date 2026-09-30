# PZ: инфраструктура и наблюдаемость

Инфраструктура работает под **Terraform и Kubernetes (k3s)** с отдельными
бюджетами CPU, RAM и диска для игры, панели, edge и наблюдаемости. Игра загружена
на сохранённом мире; все сервисы Ready, 139 итоговых проверок прошли. Облачные
ресурсы остались **4 vCPU / 16 GiB / 60 GiB**. VM, SSD и security group
находятся под Terraform, пересоздания и увеличения ресурсов не было.

Панель обновлена до **1.4.0**, включено ежечасное обновление стабильных релизов
через Kubernetes CronJob с проверенной полной копией данных и контролем версии.
Игровой контейнер при обновлении панели не перезапускался. Monium получает
метрики и логи; dashboard и дисковый alert учитывают Kubernetes и отдельные
файловые системы. Проверенный полный архив перед миграцией хранится на этой же
VM — копии для восстановления после потери всей VM пока нет.

Порядок развёртывания, границы квот, размещение state и безопасный откат описаны в [операторском runbook Kubernetes](kubernetes/README.md). Исходники теперь находятся в локальной ветке `chore/pz-k3s-iac` репозитория `updatingspace/infra`. Старый Compose сохранён в `legacy/compose` только как исторический материал. Автоматическая доставка из GitHub пока не включена; план — в [DELIVERY.md](../../docs/DELIVERY.md).

## Где источник конфигурации

| Путь | Назначение |
| --- | --- |
| `kubernetes/cloud/` | Существующие VM/SSD и применённая SG; CPU/RAM/диск, отдельный защищённый state |
| `kubernetes/storage/` | Ограниченные ext4 файловые системы окружений после проверенного backup |
| `kubernetes/bootstrap/` | Закреплённый k3s, systemd job с проверкой NodeReady, ресурсы ОС/системы |
| `kubernetes/platform/` | Namespaces, ResourceQuota, LimitRange, базовая сетевая изоляция |
| `kubernetes/workloads/` | StatefulSet игры, независимая панель, Caddy, retained PV/PVC |
| `kubernetes/panel-updater/` | Автообновление официальных стабильных образов панели, offline backup и проверка готовности |
| `kubernetes/observability/` | OTel в Kubernetes, kubelet metrics, доставка Monium |
| `runtime/game/` | Исходник действующего игрового runtime и Dockerfile |
| `legacy/compose/` | Исторические Compose, старый collector и скрипты; не применять к Kubernetes |
| `kubernetes/observability/otel-collector.yaml` | Действующий OpenTelemetry collector |
| `observability/dashboards/*.json` | Точные определения трёх действующих дашбордов Monium |
| `terraform/main.tf` | Роль отправителя логов, отдельный ограниченный API-ключ |
| `terraform/dashboards.tf` | Применение определений существующих дашбордов через адаптер официального gRPC API |
| `terraform/alerts.tf.json` | Пять правил алертов в формате Terraform JSON; желаемая конфигурация |
| `observability/alerts/applied.json` | Идентификаторы созданных алертов и способ применения |
| `scripts/` | Monium Terraform wrapper и проверка/применение дашбордов |
| `docs/operations.md` | Пороги, поиск логов, Telegram, ограничения и дальнейший перенос |

Прежний локальный `infrastructure` оставлен ссылкой на этот каталог; `stack` и `monitoring` указывают в `legacy/compose`. Второй копии исходников нет. На VM сохраняется `/opt/pz-stack` с данными и секретами. Рабочие Terraform root Kubernetes размещаются в `/opt/pz-infrastructure/{platform,workloads,observability}/`; их state независим от Monium/IAM root ниже.

## Честная граница автоматизации

У Yandex provider 0.228.0 нет ресурса Monium alert. Его `yandex_monitoring_dashboard` также не умеет корректно воспроизводить новые `multi_source_chart` в отдельном проекте Monium. Поэтому:

* IAM применяется штатным Yandex provider.
* Дашборды обновляются **на месте** из Terraform через `terraform_data` и официальный `yandex.cloud.monitoring.v3.DashboardService`. Адаптер проверяет etag и результат чтением. Удаление Terraform-записи не удаляет облачный дашборд.
* Пять алертов созданы и проверены в Monium UI. Terraform хранит их декларации, но **пока не создаёт и не обновляет алерты в облаке**. Изменение `alerts.tf.json` требует явной синхронизации через UI. В `applied.json` отражается выполненное применение.
* `terraform plan` не обнаруживает правки дашбордов в UI автоматически. Перед plan нужно выполнить `dashboard.py check`. Это переходный адаптер, а не полноценный ресурс провайдера с импортом и refresh.

Поддержка провайдера: https://yandex.cloud/ru/docs/monium/tf-ref . Нельзя заменять существующие custom-project дашборды обычным ресурсом, теряя новые виджеты и проект.

## Применение Monium/IAM

К Kubernetes эти команды не относятся: новые слои применяются по [отдельному runbook](kubernetes/README.md). Старый helper архивирован как `legacy/compose/deploy-observability.py.disabled`; после переключения на Kubernetes его не запускать.

Нужны Terraform 1.6+, Python 3, PyYAML (для развёртывания), `yc` с профилем владельца и `grpcurl`. Проверено с Terraform 1.16.3, Yandex provider 0.228.0, grpcurl 1.9.3. Закреплён `.terraform.lock.hcl`. В текущей рабочей сессии бинарники установлены в `/tmp/pz-infra-tools` (для команд ниже: `export PATH="/tmp/pz-infra-tools:$PATH"`); для постоянного использования установите их из официальных источников в PATH.

```bash
cd environments/pz
scripts/tf.sh init
for dashboard in observability/dashboards/*.json; do
  python3 scripts/dashboard.py check "$dashboard" || exit
done
scripts/tf.sh plan -out=reviewed.tfplan
scripts/tf.sh apply reviewed.tfplan
```

При намеренном изменении JSON команда `check` покажет отличие; сверить ожидаемую правку, затем plan/apply. Для исправления только UI-дрейфа можно вызвать `dashboard.py apply FILE`; сам Terraform без изменения хеша повторное применение не запустит. Сначала сохранить внешние UI-правки через `dashboard.py export COPY --id ID`, чтобы не потерять их.

## State и секреты

Terraform state **этого Monium/IAM root** локальный, в `terraform/terraform.tfstate`, с правами `0600`; он содержит секрет API-ключа. Cloud/bootstrap/storage имеют отдельные локальные state, platform/workloads/observability — отдельные state на VM; их размещение указано в Kubernetes runbook. State, plan, локальные tfvars и ключи исключены `.gitignore`. Не добавлять их в будущий Git. Сделать защищённую резервную копию state до переноса рабочего каталога. Потеря state не отзывает уже выданные ключи.

IAM-токен получается `yc iam create-token` и передаётся через окружение. Ключ логов передан в Kubernetes Secret `observability/monium-env`; исторический env остаётся в приватном архиве Compose на VM, без публикации в Git. У ключа только `yc.monium.logs.write`, срок до **18 сентября 2027 UTC**. Метрики используют прежний отдельный ключ. `prevent_destroy` предотвращает случайную замену ключа обычным apply; ротация требует отдельного плана и проверки доставки.

Telegram подключён через политику эскалации `pz-game-sms` ко всем пяти рабочим алертам: игра/сборщик, HTTP-панель, TPS, диск и JVM heap. Уведомления запускаются при ALARM, круглосуточно, без начальной задержки, с повтором через 30 минут, максимум 10 итераций до восстановления или остановки эскалации. Четыре новые подписки и прямые ссылки `alert_url` сохранены и проверены в Monium UI 30 сентября 2026; существующая подписка игры также проверена. В истории рабочей эскалации игры Telegram принял уведомление 30 сентября в 19:11 МСК; нового синтетического теста и нового подтверждения получения пользователем нет. Историческая проверка доставки от 19 сентября сохранена отдельно. Подробности — в `observability/alerts/applied.json` и `docs/operations.md`. Старый SMS-канал отключён от алертов. Terraform хранит контракт, но не применяет настройки эскалации автоматически.
