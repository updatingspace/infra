# PZ: Kubernetes и управление ресурсами

**Статус на 30 сентября 2026: миграция завершена.** Игра, панель,
Caddy и collector Ready; итоговая проверка фактических лимитов, томов и состояния
прошла 139 проверок без ошибок. Игра запущена на сохранённом мире, RCON и публичный
UDP 16261 проверены. Метрики доходят в Monium, dashboard и дисковый alert обновлены.
Финальные планы cloud/bootstrap/storage/platform/workloads/observability — no-op;
Monium-декларации также согласованы. После последующей очистки legacy свободно около
4.03 GiB в PZ filesystem и 18.68 GiB на корневом разделе (см. [отчёт](CLEANUP-20260930.md)). ПЗ-раздел занят примерно
на 84%, поэтому дисковое предупреждение с порогом 80% ожидаемо и не отключено.

Панель обновлена с 1.3.7 до **1.4.0**. Включён Kubernetes CronJob проверки
стабильных релизов каждый час на 17-й минуте. Полный backup панели, SHA256 образа,
проверка Ready/версии и переключение трафика выполняются автоматически; обновление
не перезапускает игру. Повторный запуск с текущей версией не меняет сервисы.
[Устройство и обслуживание updater](panel-updater/README.md).

Конфигурация [телеметрии панели](panel-telemetry/README.md) заменяет HOST-карточки
показателями игрового контейнера: CPU относительно лимита 2.2 CPU, RAM working
set относительно 10 GiB и отдельный фактический JVM heap с максимумом 8 GiB.
При недоступных или устаревших метриках адаптер не подставляет показатели хоста
или панели. При обновлении проверяется совместимость адаптера через health;
несовместимая версия не получает трафик и проходит штатный откат updater.

Upstream-кнопки Start/Stop/Restart ещё не адаптированы к Kubernetes: например,
RCON quit при replicas=1 приводит к новому запуску контейнера. Для остановки игры
использовать описанный ниже штатный порядок Kubernetes. Обновление панели само
по себе не добавляет эту интеграцию.

Целевая машина — `compute-vm-2-6-60-ssd-1785610759198`, адреса `10.130.0.30` /
`51.250.40.253`, зона `ru-central1-d`. Облачная конфигурация осталась **4 vCPU,
16 GiB RAM, SSD 60 GiB**; ОС показывает около **15992 MiB RAM**. Увеличение VM
в этой миграции не выполняется. Один узел k3s не обеспечивает отказоустойчивость
при потере VM или диска.

## Слои и порядок применения

Каждый каталог — отдельный Terraform root со своим state. Проверены Terraform
**1.14.7**, Yandex provider **0.228.0**, Kubernetes provider **3.2.1**.
K3s закреплён на **v1.36.4+k3s1**, версия и SHA256 установщика заданы в bootstrap.

| Шаг | Каталог | Что он контролирует | Где выполняется и хранит state |
|---|---|---|---|
| 1 | [cloud](cloud/README.md) | Существующие VM/boot disk, CPU/RAM/диск и security group | Рабочая машина, отдельный state `0600`; импорт и SG apply выполнены, повторный plan — no-op |
| 2 | `storage/` | Ограниченные ext4 файловые системы для данных; только после проверенной резервной копии | Terraform на рабочей машине, действия на VM через SSH; отдельный локальный state `0600` |
| 3 | `bootstrap/` | Закреплённый k3s, настройки kubelet, частный API | Terraform на рабочей машине, установка на VM через SSH; отдельный локальный state `0600` |
| 4 | `platform/` | Namespaces, ResourceQuota, LimitRange, Pod Security, базовые NetworkPolicy | На VM: `/opt/pz-infrastructure/platform/` |
| 5 | [workloads](workloads/README.md) | Игра, панель, Caddy, Services, локальные PV/PVC, прикладные NetworkPolicy | На VM: `/opt/pz-infrastructure/workloads/` |
| 6 | [observability](observability/README.md) | OTel collector, минимальный RBAC, сбор и доставка метрик/логов | На VM: `/opt/pz-infrastructure/observability/` |

Рабочие каталоги на VM содержат собственные `terraform.tfstate` и приватные tfvars;
права state, plan и tfvars — `0600`. Сначала проверяется план одного слоя, затем
применяется именно сохранённый план. `environments/pz/terraform/` остаётся отдельным
root для IAM и Monium; его state нельзя объединять с перечисленными слоями.

Облачный apply создал только SG и изменил её привязку к NIC; VM и диск не
пересоздавались, ёмкость не менялась. Bootstrap и storage используют явные
адаптеры `terraform_data`: Terraform хранит входы и запускает
скрипт при применении, но не обнаруживает автоматически все изменения systemd,
файлов и mount на VM. Их фактическое состояние проверяется командами на узле.
Удаление записи из state не является штатным способом удаления кластера или данных.

Bootstrap выполняется отдельным systemd unit `pz-k3s-bootstrap-SHA256.service`;
обрыв SSH не прерывает установку. Helper до 20 минут переподключается за статусом,
без автоматического перезапуска failed job. Успех требует exit 0 и текущего
NodeReady. Устранена гонка регистрации узла: bootstrap ждёт появления Node и
Ready до 180 секунд, вместо немедленной ошибки `NotFound` после установки.
Приватный лог находится в `/var/lib/pz-bootstrap/job/bootstrap.log` на VM.

## Бюджет CPU и RAM

`platform.allocatable` — бюджет приложений после ОС, k3s и системных pod,
а не весь размер VM. Текущий вход: **3000m CPU / 12000 MiB RAM**. Нельзя просто
заменить его на облачные 4 CPU / 16 GiB: предварительно нужно сверить реальные
`Node.status.allocatable` и requests системных и любых неуправляемых workloads.

| Namespace | CPU requests / limits | RAM requests / limits | PVC requests quota | Назначение |
|---|---:|---:|---:|---|
| `zomboid` | 2000m / 2600m | 8512 / 11136 MiB | 26 GiB | Игра, панель и job её автообновления |
| `edge` | 100m / 200m | 128 / 256 MiB | 256 MiB | Caddy и TLS state |
| `observability` | 100m / 200m | 256 / 512 MiB | 512 MiB | OTel; текущие offsets используют hostPath, а не PVC |
| Всего limits | **3000m** | **11904 MiB = 11.625 GiB** | **26.75 GiB** | Входной бюджет оставляет 96 MiB неприсвоенной RAM |

Лимиты контейнеров: игра **2200m CPU / 10 GiB RAM**, панель **300m / 768 MiB**,
job автообновления панели **100m / 128 MiB**, Caddy **200m / 256 MiB**,
collector **200m / 512 MiB**. Для updater зарезервированы 64 MiB requests;
лимит CPU namespace остался 2600m за счёт снижения лимита панели на 100m. JVM игры получает
`Xmx=8 GiB`, оставляя запас внутри 10 GiB для native memory и прочих расходов.
Прежний живой лимит игры 16 GiB вместе с соседними сервисами превышал память VM;
новая конфигурация устраняет это превышение на уровне заданных maxima.

ResourceQuota ограничивает суммарные заявленные requests/limits в namespace;
контейнерные limits применяются kubelet. CPU limit может вызвать throttling,
превышение RAM — OOM kill. LimitRange добавляет defaults для контейнеров без
явных ресурсов. Изменение defaults не меняет уже запущенные контейнеры.
[Официальное описание ресурсов](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
и [квот](https://kubernetes.io/docs/concepts/policy/resource-quotas/).

Terraform отклоняет план при сумме namespace CPU/RAM limits выше application
budget, requests выше соответствующих limits, некорректных defaults и пустом
наборе окружений. Это жёсткие precondition/validation, не предупреждающий `check`.
Namespaces защищены `prevent_destroy`. Minecraft и sandbox пока не включены:
для них после увеличения ёмкости добавляются собственные namespaces, квоты,
данные и workloads, с повторным расчётом общего бюджета.

## Дисковая изоляция и сохранность

Слой storage готовит отдельные ext4 файловые системы: **26 GiB для PZ,
256 MiB для edge, 512 MiB для observability**. Файлы backing storage лежат в
`/var/lib/pz-volumes/{zomboid,edge,observability}.ext4`, точки подключения —
`/srv/pz-storage/{zomboid,edge,observability}`.
Существующие пути `/opt/pz-stack/data/*` сохраняются через ссылки на соответствующие
подкаталоги подключённых файловых систем; это позволяет откатиться к Compose
на актуальных данных без второй копии рабочего мира.

Размер каждой файловой системы ограничивает объём окружения; доступное для
файлов место меньше номинального размера из-за metadata и журнала ext4.
Заявленные PVC игры: binaries 11 GiB, world 12 GiB, Steam 1 GiB, panel 1 GiB,
panel logs 1 GiB. Два PVC Caddy заявляют по 128 MiB. Внутри общей файловой системы
окружения эти PVC не получают самостоятельных жёстких квот: подкаталоги делят
один объём. `requests.storage` и размер local PV сами по себе не ограничивают
фактические записи. [Свойства local volumes](https://kubernetes.io/docs/concepts/storage/volumes/#local).

Backing-файлы sparse: их логический размер не резервирует свободные блоки
корневой файловой системы заранее. Заполнение корневого раздела может нарушить
работу всех трёх окружений даже при свободном месте внутри отдельных ext4. Все
файловые системы остаются на одном SSD 60 GiB, делят его I/O и общий домен отказа.
Нужен запас корневого раздела для ОС, образов containerd, журналов и резервных копий. Ephemeral-storage quota не ограничивает local PV или hostPath.
При заполнении PZ-файловой системы остановить рост/сервис и разбирать причину;
не освобождать место удалением файлов мира или backing `.ext4`.

В шаблоне игры ephemeral-storage **request 1536 MiB / limit 3 GiB**; у панели —
**128 / 512 MiB**. Вместе с updater (16 / 64 MiB) заявлено 1680 MiB requests и 3648 MiB limits при quota
namespace `zomboid` **2048 / 4096 MiB**. Этот бюджет учитывает writable layer,
контейнерные журналы и временные pod volumes; он отделён от **26 GiB** файловой
системы постоянных данных. Превышение ephemeral limit вызывает eviction, а не
расширение filesystem. Запас места нужно проверять и внутри PZ mount, и на `/`.

Адаптер ведёт журнал `/var/lib/pz-volumes/migration.json`, проверяет UUID и
backing-файл mount и устанавливает зависимости старта Docker/k3s от подключённых
файловых систем. `release_verified_sources=false` по умолчанию сохраняет прежние
каталоги `*.pre-k3s`. На текущем SSD для завершения переноса требуется их освобождение:
режим `true` удаляет старый источник только после проверки SHA256 архива и
побайтового rsync checksum-сравнения скопированных данных. Не удалять эти каталоги
вручную и не менять размеры образов повторным apply: рост — отдельное обслуживание.

Точка возврата перед миграцией —
`/var/backups/pz-k3s-20260930/pz-stack.tar.gz` и результат проверки архива в
`verified.json` рядом. Эта копия находится **на той же VM**, поэтому не защищает
от потери SSD/машины. Для восстановления после такой потери нужна отдельная
проверенная копия вне VM. Не восстанавливать старый архив поверх более нового мира
без намеренного выбора этой точки восстановления.

### Инцидент первого запуска: временные ZIP и история backup

Первоначальный ephemeral limit игры **1 GiB** вызвал повторные eviction во время
startup-backup. В установленном `projectzomboid.jar` подтверждены вызовы
`ZipBackup` → `ParallelScatterZipCreator()` → `DefaultBackingStoreSupplier` →
`Files.createTempFile`, с префиксом `parallelscatter`. Такой default supplier
использует временный каталог JVM; переопределения `java.io.tmpdir` не обнаружены.
Это объясняет потребность backup во временном диске вне PV. При успешном старте
с 3 GiB наблюдаемый максимум `/tmp/parallelscatter*` составил 1 413 668 864 байт
(около 1.317 GiB allocated). После старта временные файлы полностью исчезли
в том же контейнере; игра стала Ready без перезапусков. Замеры каждые 15 секунд
не гарантируют обнаружение кратковременного абсолютного пика и не измеряют весь
writable layer.
[Исходник Apache Commons Compress](https://commons.apache.org/proper/commons-compress/jacoco/org.apache.commons.compress.archivers.zip/ParallelScatterZipCreator.java.html).

После неудачных стартов в `backups/startup` остались четыре пустых ZIP и один
прежний ZIP размером 1,498,948,936 байт. Для восстановления только этой истории
подготовлен [restore-startup-backups.py](restore-startup-backups.py). Он читает
проверенный архив с member prefix `pz-stack/data/zomboid/backups/startup`, требует
ровно `backup_1.zip` … `backup_5.zip`, проверяет полный SHA256 исходного gzip,
SHA256 readback каждого staging-файла и ZIP central directory. Содержимое `Saves`
не извлекается и не заменяется.

Процедура требует replicas=0, отсутствия игрового pod, включая Terminating, и
остановленной старой Docker-игры. Staging создаётся на той же PZ filesystem;
перед каждым файлом проверяется место с запасом 512 MiB. Без `--activate`
остаётся только staging; с ним после всех проверок текущий каталог сохраняется
как `startup.before-recovery-UTC-PID`, затем staging переименовывается в `startup`.
Повторный запуск создаёт новый staging, а не продолжает прежний: перед ним снова
проверить место и существующий audit, не запускать две recovery job одновременно.
Audit находится в `/var/lib/pz-backup-recovery/*.json`, права `0600`.
Успех требует нормального exit 0 recovery job и audit `phase=complete`; запуск
задания или наличие staging этого не доказывает. До подтверждения не запускать
игру повторно и не удалять сохранённый каталог. Эта процедура восстанавливает
историю ZIP, а не возвращает мир к состоянию до миграции.

Выполнение 30 сентября завершено успешно: восстановлены и проверены все пять ZIP,
audit `20260930T161538Z-2421045.json`, `phase=complete`. После этого игра
запущена с исправленным лимитом, Ready подтверждён в 16:35 UTC; RCON сообщает
0 игроков. Публичный UDP 16261 ответил A2S_INFO. Подключение настоящего игрового
клиента по 16262 этой проверкой не выполнялось.

После успешного запуска освобождена только избыточная папка
`startup.before-recovery-20260930T161538Z-2421045`: четыре пустых ZIP и один
ZIP, SHA256 которого повторно совпал с audit архива и текущим `backup_2.zip`.
Текущая история startup, игровой мир и полный архив сохранены. Освобождено
около 1.394 GiB; доказательства находятся в приватных отчётах миграции.

## Доступ, секреты и сеть

API слушает частный адрес **`https://10.130.0.30:6443`**. Operator kubeconfig
находится только на VM: **`/etc/rancher/k3s/operator.yaml`**, права `0600`.
Рабочий контекст в нём — `default`. Private bind сам по себе не закрывает API:
облачный one-to-one NAT направляет публичный трафик на частный IP. Доступ снаружи
ограничивает применённая SG **`enpp121nmc0ggbt5hd5g`**; API `6443` и kubelet
`10250` в ней не разрешены. Администрирование выполняется по SSH, credentials
остаются на VM.
В интерактивной административной сессии на VM:

```sh
sudo -i
export KUBECONFIG=/etc/rancher/k3s/operator.yaml
kubectl get nodes -o wide
kubectl get pods -A
```

Прикладные namespaces используют Pod Security `baseline`, закреплённый на
`v1.36`, и default service account без автоматического токена. Для доверенного
collector задано исключение namespace `privileged` из-за hostPath host metrics;
его контейнер не privileged, сбрасывает capabilities и не использует hostNetwork.
Этому namespace нельзя выдавать обычным приложениям права создавать pod.

У игры и Caddy ServiceAccount token не монтируется. При включённой телеметрии
панель получает projected token отдельного `panel-telemetry` ServiceAccount:
только GET core Pod и metrics Pod с именем `zomboid-0` в namespace `zomboid`.
Нет list, Secret API, exec или прав изменения. Полный operator kubeconfig панели
не передаётся. У updater отдельный ServiceAccount с правами управления панелью;
его границы доверия описаны в [runbook](panel-updater/README.md#доверие-и-пределы-проверки).

Platform задаёт default-deny ingress/egress, обмен внутри одного namespace и DNS
только к CoreDNS в `kube-system`. Прикладные root добавляют необходимые разрешения:
edge→panel, collector→game/panel/kubelet, panel→Kubernetes API для телеметрии
и внешний доступ Steam/HTTP(S)/Monium. Дополнительный egress панели ограничен
адресами Kubernetes Service `10.43.0.1:443` и API `10.130.0.30:6443`.
Стандартная NetworkPolicy фильтрует адреса и порты, а не доменные имена.
Проверять её работу нужно фактическими соединениями, а не наличием объекта в API.

SG разрешает публичные TCP **22/80/443**, UDP **443/16261/16262** и ICMP;
исходящий IPv4 разрешён. Порты игры — UDP **16261/16262**, Caddy — TCP **80/443**
и UDP **443**. RCON
`27015`, метрики `9090` и HTTP панели `3001` остаются внутри кластера. K3s
ServiceLB занимает host ports своими системными pod, поэтому старые Compose game
и Caddy должны освободить соответствующие порты до переключения. Traefik и
автоматический local-storage provisioner отключены. На одном узле используется
Flannel host-gw: публичный VXLAN-порт не нужен. Kubelet слушает частный адрес.
ServiceLB при externalTrafficPolicy=Local также использует автоматически
выделенные NodePort для внутреннего маршрута к сервисам; SG не пропускает
обращения к этим портам из интернета.

Secrets `zomboid/pz-runtime`, `zomboid/pz-panel`, `edge/caddy-runtime` и
`observability/monium-env` создаются отдельным защищённым переносом уже имеющихся
значений. Terraform использует их имена, не читает значения в свой state.
Не печатать `kubectl get secret -o yaml`, `docker inspect` environment, kubeconfig
или полный state/plan JSON. Стандартный `kubectl get secrets -A` показывает только
имена и допустим для проверки наличия.

## История первого запуска и текущая проверка

Следующие два абзаца относятся к завершённой миграции. `import-images.py` и
`sync-secrets.py` не запускать при обычной доставке: первый перезаписывает
текущие tfvars, второй использует старые миграционные снимки. После удаления
Docker-контейнеров эти источники больше не являются рабочим способом импорта.

До первоначального storage/bootstrap требовалось сохранить мир, чисто остановить Compose игру, панель,
Caddy и collector, проверить отсутствие писателей и верифицировать backup.
Автоматический запуск старых Compose сервисов должен быть отключён. При переносе
сохранить UID/GID и права данных.

**`import-images.py` выполнять после полного завершения storage и освобождения
проверенных исходных копий.** При временной двойной копии данных kubelet GC уже
удалил импортированные, но ещё неиспользуемые образы из containerd. После storage
нужен повторный импорт под неизменяемыми migration tags и проверка наличия образов
перед workloads apply: `imagePullPolicy: Never` запрещает их автоматическую загрузку.

После подготовки storage и Ready-узла сравнить capacity и mount:

```sh
kubectl describe node compute-vm-2-6-60-ssd-1785610759198
findmnt -R /srv/pz-storage
df -h / /srv/pz-storage/*
kubectl get pods -n kube-system -o wide
k3s ctr images list
```

Для platform и observability команды выполняются на VM в каталоге слоя:

```sh
cd /opt/pz-infrastructure/platform
umask 077
export TF_CLI_CONFIG_FILE=/opt/pz-infrastructure/terraform.rc
terraform init -lockfile=readonly
terraform validate
terraform plan -out=reviewed.tfplan
terraform apply reviewed.tfplan
```

На VM registry.terraform.io недоступен: используется filesystem mirror публичного
Kubernetes provider 3.2.1 в /opt/pz-infrastructure/provider-mirror; его содержимое
проверяется закреплённым .terraform.lock.hcl. Пример CLI-конфигурации —
terraform.rc.example. deploy.py sync передаёт только исходники, plan/apply
выполняют Terraform на VM. sync-secrets.py переносит значения только внутри VM;
import-images.py импортирует точные сохранённые образы и записывает tfvars.

`python3 environments/pz/kubernetes/deploy.py apply STAGE` запускает Terraform
в отдельном systemd unit `pz-terraform-STAGE-SHA256.service`: SSH можно потерять,
применение продолжится на VM. SHA256 относится к проверенному `reviewed.tfplan`;
для запуска сохраняется отдельная неизменяемая копия в `STAGE/.apply/SHA256/`.
Лог `apply.log` остаётся там же на VM с правами `0600`. Helper до 20 минут
переподключается только за статусом и подтверждает успех лишь по завершению unit
с кодом 0; timeout не останавливает job. Повтор той же команды с прежним планом
подключается к этому же unit. Failed job автоматически не перезапускается;
исчезнувший unit после попытки запуска требует проверки на VM, в том числе после
reboot. Новый проверенный план получает новый hash/unit. Пока apply работает,
не выполнять sync/init/plan этой stage и не менять её рабочий каталог.

Для workloads применять только `deploy.py plan workloads` / `deploy.py apply workloads`: helper приостанавливает updater, ждёт завершения его Jobs/Pods и проверяет
журнал перед планом и apply. Нельзя параллельно запускать ручной updater Job.
При брошенном плане расписание останется на паузе; успешный apply возвращает
настроенное значение. Подробности — [автообновление панели](panel-updater/README.md).
Read-only проверку no-op можно выполнить обычным `terraform plan -detailed-exitcode`
без сохранения плана, когда updater не работает. Такой план не применять.

Перед workloads должны существовать secrets и доступные подключённые пути PV.
Применение остальных слоёв выполняется в их собственных каталогах; не копировать
между ними state. После применения:

```sh
kubectl get resourcequota,limitrange,networkpolicy -A
kubectl get pv
kubectl get pvc -A
kubectl -n zomboid wait --for=condition=Ready pod/zomboid-0 --timeout=30m
kubectl -n zomboid rollout status deployment/panel --timeout=5m
kubectl -n edge rollout status deployment/caddy --timeout=5m
kubectl -n observability rollout status deployment/otel-collector --timeout=5m
kubectl get pods -A -o wide
kubectl get events -A --sort-by=.lastTimestamp
kubectl -n zomboid exec zomboid-0 -- python3 /usr/local/bin/pz-game rcon players
```

Проверить внешний HTTPS панели с действующим TLS, вход в панель и сохранение её
данных, загрузку прежнего игрового мира и подключение игрового клиента по UDP.
Само наличие Service/Ready pod не доказывает внешнюю доступность UDP игры.
Проверить отсутствие OOMKilled, eviction, CrashLoop и ошибок quotas, PV или DNS.
Проверить размер mount и свободное место всех трёх файловых систем.

Collector Ready не доказывает доставку. Нужны свежие host, game, panel и Kubernetes
ряды в Monium, новые логи, рост sent counters и отсутствие failed/refused/накопления
очереди. Изменение схемы метрик Docker→kubelet и диагностика описаны в
[runbook наблюдаемости](observability/README.md). После стабилизации повторный
`terraform plan -detailed-exitcode` для каждого применённого слоя должен быть
no-op; сохранять только сводку проверки без секретов. Эти подтверждения выполнены для текущей миграции;
итоговые факты записаны в [migration-status.json](migration-status.json),
полные безопасные отчёты — в `../private/k3s-migration/` с правами `0600`.
Свежая доставка метрик подтверждена отдельным 90-секундным окном; новых логов
в этом окне не было, первоначальный экспорт логов проверен отдельно.
Подключение полноценного игрового клиента по UDP 16262 остаётся непроверенным.

## Последующее увеличение ресурсов

Размер VM и бюджеты приложений меняются отдельно. Владелец увеличивает облачный
SSD **60 → 100 GiB самостоятельно**; это изменение ещё не подтверждено и здесь
не выполнено. После подтверждения фактической ёмкости отразить `boot_disk_gib=100`
в приватных переменных `cloud` до следующего plan: старый desired state иначе
будет предлагать вернуть прежний размер. Будущие изменения `vm_cores` и
`vm_memory_gib` также требуют отдельного проверенного плана.
`allow_stopping_for_update` по умолчанию выключен; изменение, требующее остановки
VM, выполняется в отдельное окно после сохранения и штатного завершения игры.

После увеличения облачного диска отдельно проверить размер раздела и корневой
файловой системы. Рост boot disk не увеличивает автоматически ext4-образы
окружений. Отдельный [storage-growth](storage-growth/README.md) подготовлен для
роста PZ **26 → 50 GiB, но ещё не применён**. Он требует проверенного backup,
подтверждённых размеров SSD/раздела/root ext4 и запаса места; только увеличивает
существующие backing file, loop и ext4. PV/PVC и storage quotas учитываются
отдельно и этим helper не меняются. Исходный `storage/` и его state остаются
историей миграции: повторно запускать перенос или менять его размеры нельзя.

После роста CPU/RAM проверить фактический Node allocatable и системные requests,
затем изменить application budget и квоты `platform`, а после них — ресурсы
workloads. Для Minecraft потребуется свой namespace, бюджет, постоянные данные
и workload. Новые лимиты игры вступают в силу после штатного пересоздания её pod;
из-за `OnDelete` один Terraform apply сам по себе игру не перезапустит.

## Обслуживание и откат

Игра — один StatefulSet с `OnDelete`: изменение шаблона через Terraform само
по себе не перезапускает текущий pod. Для короткого перезапуска удаление pod
обычным способом запускает новый экземпляр из текущего шаблона. Для согласованной
резервной копии, восстановления или rollback сначала снизить replicas до нуля:
простое удаление pod при replicas=1 немедленно создаст нового писателя.

PID 1 игры обрабатывает SIGTERM: отправляет RCON `save`, затем `quit` через
`127.0.0.1` и ждёт завершения дочернего процесса. Если RCON ещё недоступен,
он передаёт `save`/`quit` в stdin игрового процесса. Grace period — 300 секунд;
отсутствие pod после ожидания само по себе не доказывает чистый выход. До удаления
записи контейнера сохранить подтверждение runtime `Game process exited with code 0`
или выбранный CRI exit status 0. При timeout, exit 137, OOM или неизвестном исходе
не считать сохранение подтверждённым и не запускать второй сервер до проверки.
Не использовать `--force` или `--grace-period=0`: после истечения обычного grace
Kubernetes также может принудительно завершить процесс.
[Порядок завершения pod](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination).
Обновление панели — отдельный Deployment `Recreate`; оно не перезапускает игру.

Docker Compose выведен из эксплуатации 30 сентября 2026: контейнеры, образы
и build cache удалены из Docker; Docker service/socket и отдельный системный
containerd выключены и исключены из автозапуска. Встроенный containerd K3s
продолжает работать. Исторические конфиги закрыты в
`/opt/pz-stack/legacy-compose-20260930/` на той же VM. Рабочие PV-пути
`/opt/pz-stack/data/*` сохранены. Подробности — [отчёт очистки](CLEANUP-20260930.md).

Основной путь восстановления — актуальный Kubernetes IaC и проверенные данные.
Четыре исходных образа сохранены в компактном проверенном архиве на VM.
[Исторический план возврата Compose](../legacy/compose/RECOVERY.md) теперь
требует восстановления Docker/образов/конфигов и отдельной проверки совместимости
панели; прямой `docker compose up` больше не является готовым способом отката.

`prevent_destroy` защищает namespace, VM, disk и PV/PVC при обычном Terraform
плане, пока соответствующие resource-блоки существуют. Он не блокирует ручные
удаления через Kubernetes/cloud API и не заменяет backup.
[Семантика lifecycle](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle).
[Конфигурация k3s](https://docs.k3s.io/installation/configuration).
