# Коллектор Monium в k3s

Этот Terraform root переносит существующий OTel collector в один отдельный pod
`observability/otel-collector`. Namespace, ResourceQuota, LimitRange, DNS и базовые
NetworkPolicy создаёт соседний `platform` root. Ресурсы pod: requests 100m CPU /
256 MiB RAM, limits 200m / 512 MiB; ephemeral storage 128 / 512 MiB.

По умолчанию закреплён существующий образ contrib 0.161.0 и прежний SHA256.
Для миграции без скачивания передать `collector_image` с точным уникальным тегом
этого же образа, предварительно импортированного из Docker в containerd.
`IfNotPresent` использует локальный образ при наличии совпадающего имени.
Сохраняются проект
доставки Monium, cluster/host/service метки игровых и системных метрик,
редактирование чувствительных строк в логах, проверка панели `check.name=panel-http`,
смещения чтения и дисковая очередь. Секреты не читаются Terraform: pod использует
заранее созданный Secret `monium-env` с ключами `MONIUM_API_KEY` и
`MONIUM_LOGS_API_KEY`. Сам Secret должен создавать отдельный защищённый процесс.

## Хранилище и граница доверия

Коллектор закреплён на существующем узле. Для чтения метрик ОС он получает `/` как
read-only `/hostfs`; файлы игры и панели отдельно подключены read-only. Каталог
`/opt/pz-stack/data/otelcol` подключён на запись для прежних offsets и очереди.
Все hostPath требуют существующий `Directory`, поэтому неверный путь останавливает
pod, а не создаёт пустое хранилище. Terraform не удаляет эти каталоги при удалении
Deployment; отдельного PVC здесь нет. Указанный ephemeral-storage limit не
ограничивает объём hostPath. После миграции каталог очереди размещён через
совместимую ссылку на отдельной файловой системе `/srv/pz-storage/observability`
размером 512 MiB, которую создаёт `storage` root. Настройки collector дополнительно
ограничивают очередь; свободное место этой файловой системы наблюдается в Monium.

HostPath требует исключения из Pod Security Standards для namespace observability:
platform устанавливает `enforce=privileged`. Сам контейнер не privileged; у него
сброшены все capabilities, запрещён privilege escalation, корневая ФС read-only,
seccomp RuntimeDefault. Root UID нужен для существующих журналов и host metrics.
Такой collector относится к доверенной инфраструктуре узла; права создания pod в
этом namespace нельзя выдавать обычным приложениям. Отдельный Docker socket не
монтируется, но корневой hostPath всё равно открывает файловое дерево узла, включая
чувствительные пути. Read-only mount сам по себе не блокирует обращения к Unix
socket внутри дерева; это не граница изоляции недоверенного кода от узла.

Deployment использует `replicas=1` и `Recreate`: два collector не должны открывать
одинаковую файловую очередь. До запуска этого pod остановить Compose collector и
проверить его остановку. При откате сначала остановить Kubernetes collector, затем
возвращать Compose. Не удалять данные очереди и offsets. Копирование файлов после
остановки позволяет сохранить отдельную точку восстановления.

## Доступ и проверка

`kubelet_stats` читает `/stats/summary` у HTTPS kubelet по `status.hostIP:10250`.
Выделенная service account имеет только `get nodes/stats`; ни secrets, ни
`nodes/proxy`, ни управления pod ей не предоставлено. TLS проверяется стандартным
CA из service account. Если конкретный kubelet имеет другой CA, установить его
как отдельный несекретный ConfigMap и указать `ca_file`; не отключать проверку без
подтверждённой причины. Дополнительное API discovery и `k8sattributes` здесь не
нужны: summary уже содержит имена node/namespace/pod/container.

NetworkPolicy допускает игровые метрики 9090 и health панели 3001 в namespace
zomboid, kubelet 10250 только на private IP узла и внешний TCP 443 с исключением
частных/локальных/metadata диапазонов. Стандартный NetworkPolicy не умеет разрешать
только DNS-имя Monium; это ограничение текущей HTTPS egress policy. DNS разрешает
platform. Входящего публичного порта collector нет. ClusterIP 8888 доступен для
внутренней диагностики; readiness/liveness используют 13133.

```sh
terraform init
terraform fmt -check
terraform validate
terraform test
terraform plan -var-file=/secure/path/observability.tfvars
# После создания platform, Secret, подготовки hostPath и остановки старого collector:
terraform apply -var-file=/secure/path/observability.tfvars
kubectl -n observability rollout status deployment/otel-collector
kubectl -n observability logs deployment/otel-collector --tail=100
kubectl -n observability port-forward service/otel-collector 8888:8888
```

Перед применением проверить YAML командой `otelcol-contrib validate` именно из
закреплённого образа с временными несекретными значениями env. После применения
проверить отсутствие TLS/RBAC/network ошибок, наличие `k8s.*` и `container.*`
метрик, рост `otelcol_exporter_sent_metric_points` и
`otelcol_exporter_sent_log_records`, отсутствие failed/refused, очередь и доступность
игровых метрик и HTTP панели. Readiness collector сама по себе не подтверждает
доставку в Monium. Host disk/filesystem scraper больше не ограничен единственным
`vda` и `/`. График заполнения в инфраструктурном дашборде показывает `/` и
`/srv/pz-storage/zomboid`, `/srv/pz-storage/edge`, `/srv/pz-storage/observability`.
Пределы отдельных файловых систем окружений: 26 GiB, 256 MiB и 512 MiB.
Легенда содержит mountpoint; графики физического I/O по-прежнему относятся к `vda`.

30 сентября 2026 в Monium UI подтверждены все четыре живых mountpoint, затем
запрос `pz-disk-usage` расширен на них и проверен повторным чтением сохранённого
алерта. Пороги 80% / 90%, ALL/GREATER, окно 10 минут, задержка 30 секунд,
строгая оценка, NO_DATA и отсутствие каналов уведомлений сохранены. Проверка
запроса вернула OK (65,66%). ID — `mond4r1ecj79b4qh7g11_pz-disk-usage`;
результат записан в `observability/alerts/applied.json`. Terraform хранит контракт,
но provider по-прежнему не применяет правила алертов.

## Изменение контейнерных графиков

Docker receiver заменён kubelet receiver, метка новых контейнерных рядов —
`service=pz-kubernetes`. В `observability/dashboards/infrastructure.json` сохранены
ID и расположение 15 графиков; три контейнерных графика переведены на новую схему.
Исторический UI-шаблон `observability/collector/dashboards/infrastructure.json` и
его генератор тоже обновлены. Дашборд применён через Terraform, точное определение
проверено повторным чтением API; в Monium UI подтверждены новые CPU/RAM-ряды
подов и uptime. Используются следующие соответствия:

| Старый ряд | Новый ряд и смысл |
|---|---|
| `container.cpu.usage.total` (наносекунды, counter) | `container.cpu.usage` (CPU cores, gauge); процент одного ядра = 100 × значение, без derivative |
| `container.memory.usage.total` | `container.memory.usage`; для рабочей памяти отдельно `container.memory.working_set` |
| `container.name` | `k8s.namespace.name`, `k8s.pod.name`, `k8s.container.name` |
| `container.uptime` | `container.uptime` включён, но с новыми метками |
| `container.restarts` | Нет аналога в kubelet summary; нужен kube-state-metrics / Kubernetes state receiver либо отдельный сбор статуса pod |

Не переименовывать новые ряды в старые Docker-имена: единицы CPU различаются.
Исторические Docker-ряды остаются доступны. Игровые, host, panel и collector
графики сохраняют прежнюю идентичность там, где семантика не изменилась.

Документация закреплённого receiver:
[README v0.161.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.161.0/receiver/kubeletstatsreceiver/README.md),
[метрики и единицы](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.161.0/receiver/kubeletstatsreceiver/metadata.yaml).
