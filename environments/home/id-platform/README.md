# UpdSpace ID: перенос в домашний k3s

Состояние на 2026-10-09: **публичный ID перенесён на VM**.
`id.updspace.com` — proxied CNAME на `updspacedd.tplinkdns.com`; Caddy автоматически
выдаёт и продлевает сертификат ID. После приёмки переноса старые облачные
контейнеры, gateway и timers выведены из эксплуатации. Облачные данные
и конфигурация сохранены для восстановления; рабочий источник теперь VM.
Точная ведомость удаления и новая политика backup —
[cloud-retirement-2026-10-09.md](../../../docs/cloud-retirement-2026-10-09.md).
VM: `updspace_m4tveevm@192.168.1.176`, node `updspace-home`.
Portal переносится отдельной задачей и владеет PostgreSQL.

## Размещение

| Компонент | Место | Состояние |
| --- | --- | --- |
| API, sessions, mutations, web, jobs, внутренний Caddy | namespace `updspace-id`, Deployment `id` | 6/6 Ready, публичный сервис |
| Одна YDB для всего ID | `updspace-data/id-ydb`, database `/local`, TLS 2135 | 67 исходных таблиц восстановлены |
| Файлы ID и Portal | общий Garage в `updspace-data`, отдельные ключи и bucket | публичный HTTPS проверен |
| Пять фоновых расписаний ID | CronJobs в `updspace-id` | все `suspend: false` |
| Полный backup ID | зашифрованные архивы на VM | daily timer включён; копирование на ПК отключено владельцем |
| Общий HTTP/TLS edge | существующий Caddy в `edge` | принадлежит общему проекту infra |

ID использует YDB по явному решению пользователя. PostgreSQL не поддерживается
текущим Rust runtime: прямые YDB-запросы есть в 95 файлах. Роль/schema `id`,
ранее подготовленные в PostgreSQL агентом Portal, **не используются**.
Дробления ID на отдельные базы нет. Portal использует свою общую PostgreSQL.

## Ограничение выбранной YDB

[YDB требует для production физические блочные устройства от 800 GB](https://ydb.tech/docs/en/devops/concepts/system-requirements).
Текущий экземпляр использует single-node образ `local-ydb` 26.2.1.14, файл
PDisk 80 GiB на общем диске VM, PV 85 GiB. Такая конфигурация **не поддерживается
upstream для production**. Успешный перенос и restore не доказывают надёжность
диска и не заменяют отказоустойчивость. После сообщения об этом ограничении
пользователь явно разрешил переключение текущего размещения и простой.
Для поддерживаемого production потребуется другое хранилище/размещение YDB.
Rescue boot, существующий k3s и посторонние workloads не менялись.

[YDB manifest](id/ydb.yaml) закрепляет image digest, requests 500m/1Gi и limits
2 CPU/4Gi, TLS, запрет анонимного доступа, UID 65534 и сетевую изоляцию.
[Конфигурация сервера](id/ydb-config.yaml) не содержит паролей. Runtime-пользователь
`idruntime` имеет connect/read/modify без DDL. Пароли root/idruntime, TLS CA и
ключи находятся только в Kubernetes Secrets и root-only каталоге
`/opt/updspace-data/id-ydb`. Поддельный `root@builtin`, неверный пароль,
анонимный доступ и DDL от runtime проверены и отвергаются.

## Точная версия приложения

Сохранённая версия прежнего cloud production — commit `5bfa8dad20fcb2f34292bcd2481515df908ab2c9`.
Текущий рабочий checkout отличается и содержит посторонние незавершённые правки;
он не использовался как источник deployment. Исходники точного commit извлечены
отдельно, к ним применён [минимальный patch](id/runtime-local-services.patch):
операторский S3 endpoint и static YDB credentials с CA и DNS identity при TLS.
API/jobs собраны на VM на прежнем production base image. Web image сохранён
байт-в-байт. 96 unit tests и 4 HTTP S3 export tests прошли; 20 интеграционных
тестов были skipped по их исходным условиям. Полный CI не запускался.

[Application manifest](id/applications.yaml) содержит неизменные три набора API
feature flags через отдельные Secrets. Локальный образ импортирован в k3s и
закреплён digest; публикации registry не было. API limits по 1 CPU/1Gi,
web/jobs по 1 CPU/512Mi, внутренний router 500m/128Mi. YAML по умолчанию имеет
`replicas: 0`: запуск требует подготовленных Secrets и восстановленной базы.
В центральном infra текущий desired state задан overlay
`environments/home/id-platform/id/overlays/production`: replicas=1, пять CronJobs
включены. Trial overlay остаётся только исторической инструкцией для изолированной пробы.
Шесть процессов объединены в один pod, чтобы неизменный web image обращался
к API через допустимый `http://localhost:8089`.

[131 operation/81 path](id/routes.json) сняты с действующего API Gateway:
50 main API, 3 sessions, 65 mutations, 13 web. [Генератор](id/render_routes.py)
проверяет выбор компонента и приоритет exact/parameter/greedy маршрутов.
Внутренний router отдельно меняет только старый S3 origin в CSP web-ответов.
Jobs слушают отдельный приватный порт 8084 и не публикуются через router.

Из прежних Lockbox перенесены 11 именно ID application secrets: signing,
HMAC, MFA, export и SMTP. Облачные IAM/S3 credentials на VM не переносились.
Их место занимают отдельные локальные runtime credentials. Пароли пользователей,
OIDC client secrets, MFA, passkeys и сессии входят в полную копию YDB.
Issuer, WebAuthn RP ID, cookie names и публичные callback URL сохраняются.

## Данные и проверка

Финальный согласованный source snapshot от `2026-10-09T14:52:43Z` содержит
67 таблиц, 1833 строки и 19 пользователей. Он снят после 600 секунд drain и
проверенного отказа старому runtime в доступе к YDB.
[Финальный restore](id/final-restore-2026-10-09.json) заменил пробную базу целиком.
После restore выполнен повторный ordered dump: CSV всех 67 таблиц совпали
байт-в-байт. Более ранняя [статистика схем](id/source-schema-2026-10-09.json)
показывала около 2059 строк; она была несогласованной и включала другой момент TTL.
В Garage перенесены 6 объектов ID, 238025 байт; все SHA256 совпали при readback.
Export bucket на момент копирования был пуст.

[Протокол ID](id/acceptance-2026-10-09.json) фиксирует реальный TLS/DB доступ,
CSRF, ошибочный пароль, успешный вход, Secure/HttpOnly cookie, sessions route,
logout/revocation, расшифровку TOTP прежним MFA-ключом и отказ при повторном коде.
Проверки выполнялись на синтетическом отрицательном user ID в пробной базе.
[Скрипт проверки](id/verify-trial-auth.py) требует marker `TRIAL_ONLY`.
Финальный restore уже удалил синтетические account/identity/outbox-записи;
`TRIAL_ONLY` переименован в `TRIAL_REPLACED_20261009`. Пробный fixture больше
нельзя запускать на этой базе.
Публичные JWKS и OIDC discovery облака и новой сборки полностью совпадают.
Настоящий изолированный Chromium с DNS override на VM также прошёл вход
с паролем/TOTP и личный кабинет, без отключения TLS, с Secure/HttpOnly cookie
и без page errors. Browser passkeys, реальные provider redirects и доставка настоящего письма ещё
не проверялись; синтетические HTTP проверки не подменяют их.

[Проверка Portal SSO](id/portal-sso-2026-10-09.json) прошла через настоящие
локальные ID и Portal с проверкой TLS: пароль → MFA → согласие → callback →
сессия Portal. Проверены совпадение identity UUID, active membership в `aef`,
готовые права доступа и загрузка Overview без browser page errors. Это закрытая
пробная среда с синтетической учётной записью до переключения DNS.
После переключения [публичный Chromium smoke](id/public-browser-2026-10-09.json)
проверил login, CSS, health/readiness, discovery/JWKS и anonymous auth через
реальный DNS без TLS bypass. Вход настоящего пользователя после cutover не проверялся.
[Client IP acceptance](id/public-client-ip-2026-10-09.json) подтвердил IP через
Cloudflare и отказ доверять поддельным forwarding-заголовкам при прямом доступе.

## Garage и общий edge

[Garage manifest](garage/garage.yaml): версия 2.4.1 закреплена digest, PV Retain
`/srv/updspace/garage`, S3 3900 ограничен NetworkPolicy: Caddy и отдельно разрешённые сервисы Portal.
Root backup ID на VM использует внутренний endpoint и собственный ID key,
чтобы сохранять исходные object metadata без подмены Cache-Control общим edge.
RPC/admin — loopback.
Runtime keys раздельные, без owner/admin прав, каждый видит только свои bucket.
Публичный endpoint `https://storage.updspace.com`, регион `garage`, path-style.
ID buckets: `updspace-id-media-ab88348a`, `updspace-id-exports-e3dd0415`.
Portal bucket: `updspace-portal-prod-media-ab88348a`. Старый `s3.updspace.com`
принадлежит другому CDN и не менялся.

Storage CNAME указывает на `updspacedd.tplinkdns.com`, **DNS-only, TTL 60**.
Через Cloudflare proxy короткие запросы работали, PUT/GET 40KB зависали; прямой
WAN TLS успешно передаёт 10MiB. Конкретная причина ISP не доказана. После обхода
прошли SDK и presigned PUT/GET 10MiB, SHA256, CORS, а агент Portal подтвердил это
из настоящего Chromium с origin `https://portal.updspace.com`.
[Полный протокол крупных запросов](garage/public-large-acceptance-2026-10-09.json)
уточняет прежний [small-payload протокол](garage/acceptance-2026-10-09.json).
Успешны также multipart, запрет anonymous/tampered access, рестарт и отдельный
холодный restore Garage. HTTPS — Let's Encrypt, ответы private/no-store;
подписанные URL не пишутся в access log.

CORS разрешает Portal и tenant origins, GET/HEAD/PUT, content-type и exposed ETag.
Lifecycle ID exports: `exports/` 2 дня, незавершённые multipart 1 день; штатные
ID jobs сохраняют собственную очистку. Shared Garage full backup до настроек
lifecycle существует на VM; его секреты Portal не включены в ID offsite bundle.
ID backup включает только свои объекты и свой Garage key.

Canonical shared Caddy теперь находится в проекте
`/home/m4tveevm/PycharmProjects/infra/environments/home/edge`.
На VM зеркало `/opt/updspace-infra/source`; старый
`/opt/pz-infrastructure/local-edge/Caddyfile` — compatibility copy.
Общий Caddy меняется последовательно с агентом infra, с проверкой свежего
resourceVersion и сохранением PZ/status/storage/observability.

## Backup и восстановление

Первоначально пользователь выбрал рабочий компьютер, но после миграции отменил
этот вариант: существующие архивы ID/Portal с ПК удалены, offsite timers отключены.
Закрытый ключ шифрования сохранён в `~/.local/share/updspace-backups/keys`;
на VM передан лишь публичный recipient. Следующие описания offsite restore —
исторические доказательства миграции, не наличие текущей внешней копии.
Исходный cloud dump сохранён отдельно как `source-ydb.tar.gpg` и расшифрован с
проверкой SHA256. Первая финальная локальная копия `20261009T145502Z/id.tar.gpg` содержит
sparse YDB disk/config, все ID runtime/DB Secrets, CA, шесть media objects,
Garage ID key и манифесты. Размер 271869965 байт.
[Проверка финальной копии](id/final-backup-2026-10-09.json): COMMITTED, SHA256
шифротекста и расшифрованного архива, все 20 вложенных файлов и metadata шести
объектов совпали. Отдельный полный runtime restore этой финальной копии не повторялся;
изолированное восстановление прежней offsite-копии описано ниже. Свежий исходный
cloud snapshot отдельно зашифрован в `id-final-source/20261009T145250Z/source.tar.gpg`.

[backup.py](id/backup.py) блокирует наложение, сохраняет состояние Deployment и
CronJobs, останавливает только ID writers/YDB, снимает холодный sparse archive и
логический ID-only S3 snapshot, возвращает исходное состояние, затем шифрует.
Последняя остановка заняла 17.68 s. При незавершённом resume остаётся
`/srv/backups/updspace-id/resume-state.json`: его нельзя удалять без проверки.
Завершённые ciphertext/manifest/COMMITTED лежат в
`/srv/backups/updspace-id-encrypted/<UTC stamp>`, незашифрованный stage закрыт root.

[Daily timer](id/updspace-id-backup.timer) подготовлен на 02:30 UTC (05:30 MSK),
**включён на VM**, первый плановый запуск 2026-10-10 02:30 UTC. Он означает короткое ежедневное окно недоступности ID;
длительность будет расти вместе с объёмом данных. [Offsite sync](id/sync-backups.py)
ранее работал каждый час с проверкой COMMITTED/SHA256. Теперь user timer
`updspace-id-offsite.timer` disabled/inactive по прямому решению владельца;
не включать его снова без изменения этой политики. VM daily backup продолжает
работать. Копия на том же диске/VM не защищает от потери самой VM.

Проверено восстановление именно **расшифрованной off-VM копии**: 20 вложенных
файлов совпали с manifest, отдельный сетево изолированный YDB pod стал Ready,
прежний root password открыл базу, контрольная запись и 19 пользователей
восстановлены, все шесть файлов совпали по SHA256. Тестовый pod остановлен;
root-only каталог `/srv/updspace/id-restore-drill-20261009` и proof сохранены.

При аварии: расшифровать копию на рабочем компьютере; проверить plaintext SHA;
передать архив в закрытый каталог VM; проверить manifest; развернуть `ydb.tar`
с сохранением sparse-файлов/UID 65534; вернуть Secrets и TLS; запустить YDB с
сохранённой конфигурацией и `.authentication-enforced`; проверить вход/данные;
восстановить только ID S3 bucket с прежними ключами и metadata; запустить ID;
включить исходные расписания только после проверки. Новый пустой PDisk требует
отдельного изолированного bootstrap root/idruntime; один StatefulSet не создаёт
учётные записи автоматически. `YDB_DEFAULT_PASSWORD` должен быть буквенно-цифровым:
bootstrap local-ydb отвергал пароль с `-`, поэтому обёртка после bootstrap исключена.

## Переключение завершено

При переключении пять облачных timers поставлены на PAUSED, gateway отвечал 503, прямые local
invocation bindings удалены. После 600 секунд drain у прежнего runtime account
удалён только ID database grant; IAM-проверка от его имени вернула PermissionDenied.
Наследуемые роли общих cloud accounts не менялись. Финальные YDB/S3 скопированы
и проверены до открытия нового ID route. Caddy получил managed LE certificate
в 14:41 UTC, публичный ID проверен после restore в 15:03 UTC. Пять локальных
CronJobs и daily backup включены в 15:10 UTC;
[все пять заданий](id/final-schedules-2026-10-09.json) успешно отработали к 15:17 UTC.
[Итоговый протокол](id/final-cutover-2026-10-09.json) фиксирует границы проверки.
[Runtime audit](id/final-runtime-audit-2026-10-09.json) подтвердил соответствие
images/flags/router production overlay и отсутствие YC endpoints в пяти компонентах.
Исторические доказательства и
ограничения — в [CUTOVER.md](id/CUTOVER.md).

Cloudflare доверяет сертификату origin; внешний сертификат браузер↔Cloudflare
обслуживает сам Cloudflare. Origin CA возможен как отдельный выбор edge, но
не меняет срок действия установленного на сервере сертификата автоматически.
Portal TLS принадлежит отдельной задаче; его ручной сертификат этой задачей
не заменён. Управление общим edge передано владельцу Portal после проверки ID.

После первой записи на новой стороне простой DNS rollback теряет изменения:
нужна остановка writers и обратная синхронизация/восстановление. Новая сторона
уже принимала записи; старые cloud writers нельзя просто включить снова.
