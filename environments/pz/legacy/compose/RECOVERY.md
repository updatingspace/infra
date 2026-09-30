# Исторический возврат в Docker Compose

После очистки 30 сентября 2026 Docker-контейнеры и образы удалены из Docker,
Docker service/socket и отдельный системный containerd отключены. Простого
`docker compose up` больше недостаточно. Предпочтительно восстанавливать
Kubernetes из актуального IaC и retained data.

На той же VM сохранён `root:root 0600` архив четырёх точных образов
`/var/backups/pz-k3s-20260930/legacy-images.tar.gz` (395880997 bytes), SHA256
`13b99dccf0e33ad8b7bafef79105c76212f31a41f4897c719e087e24f9ef68c7`.
Рядом manifest сопоставляет image IDs и теги восстановления. Проверены tar,
46 OCI blobs, config/layers и полный SHA; пробный `docker load` не выполнялся.
Исходные приватные Compose-конфиги перемещены внутри VM в
`/opt/pz-stack/legacy-compose-20260930/` (0700). Не запускать Compose прямо
из этого подкаталога: его относительные data-пути потребуют проверки.

Для сознательного возврата нужны отдельное окно обслуживания, новая проверенная
копия актуальных данных, сверка совместимости текущей panel DB с версией образа,
восстановление тегов из manifest и только затем включение Docker. Архивная
панель 1.3.7 не гарантирует совместимость с данными панели 1.4.0 и новее.
Не восстанавливать старую panel DB поверх текущей без принятого плана потери
последующих изменений. Сначала проверить всё на отдельной копии.

Ниже сохранён исторический порядок остановки писателей и освобождения портов;
он не заменяет этих дополнительных условий и не является готовым автоскриптом.

Для отката к Compose сначала приостановить CronJob `panel-auto-update`, дождаться
завершения всех его Jobs/Pods и проверить завершённый журнал (`committed` или
`rolled_back`). Убедиться, что Terraform apply и recovery jobs не работают
и никто не вернёт replicas=1. Остановить все Kubernetes-писатели;
команды ниже выполняются на VM в административной сессии с operator kubeconfig.
Если pod уже отсутствует, подтвердить это отдельным `get`, а не игнорировать
неожиданную ошибку ожидания:

```sh
kubectl -n zomboid scale deployment/panel --replicas=0
kubectl -n zomboid wait --for=delete pod -l app.kubernetes.io/name=panel --timeout=90s
kubectl -n zomboid scale statefulset/zomboid --replicas=0
kubectl -n zomboid wait --for=delete pod/zomboid-0 --timeout=360s
kubectl -n edge scale deployment/caddy --replicas=0
kubectl -n edge wait --for=delete pod -l app.kubernetes.io/name=caddy --timeout=90s
kubectl -n observability scale deployment/otel-collector --replicas=0
kubectl -n observability wait --for=delete pod -l app.kubernetes.io/name=otel-collector --timeout=120s
kubectl get pods -n zomboid
kubectl get pods -n edge
kubectl get pods -n observability
```

Подтвердить штатное сохранение/завершение игры и отсутствие процессов панели и
collector, держащих данные/очередь. Освободить ServiceLB host ports: Services
`zomboid/zomboid-public` и `edge/caddy` временно перевести из `LoadBalancer` в
`ClusterIP`, дождаться удаления соответствующих `svclb-*` pod в `kube-system`
и проверить `ss -lntup`. Проверка сокетов дополняет проверку отсутствия ServiceLB
pod; она не заменяет её, поскольку ServiceLB использует также правила iptables.
Сам scale приложения до нуля не освобождает ServiceLB.
[Устройство k3s ServiceLB](https://docs.k3s.io/networking/networking-services#service-load-balancer).

Проверить, что `/opt/pz-stack/data/*` по-прежнему указывают на правильно смонтированные
актуальные данные. Снять и проверить новую согласованную копию текущего мира,
panel database, Steam/TLS state и offsets collector, предварительно проверив
место для неё. Читать реальные подкаталоги `/srv/pz-storage/...`: обычный tar
путей `/opt/pz-stack/data/*` без dereference может сохранить лишь symlinks.
Не копировать подключённые `.ext4` как замену согласованному backup файлов.
Pre-migration архив и `*.pre-k3s` не являются источником данных для обычного
rollback: они не содержат последующих сохранений и изменений панели.

Перед Compose проверить закреплённые имеющиеся образы, права UID/GID и его
CPU/RAM/JVM limits: прежний лимит игры 16 GiB нельзя возвращать на VM с 16 GiB
вместе с k3s и соседями. Только после всех проверок запускать проверенный Compose
из `/opt/pz-stack`, без обновления образов и сборки:

```sh
cd /opt/pz-stack
docker compose up -d --no-build --pull never zomboid panel caddy otel-collector
```

**Никогда не запускать одновременно Kubernetes и Compose против одного мира,
базы панели или очереди collector.** Во время работы Compose не выполнять
workloads/observability apply: они восстановят декларативные реплики `1`, а
workloads также вернёт публичные Services. После выбора постоянного варианта
привести Terraform-конфигурацию в соответствие с ним. Для возврата в Kubernetes
сначала штатно остановить Compose и проверить отсутствие его писателей и
занятых host ports; затем использовать эти же актуальные данные и проверенный plan.
