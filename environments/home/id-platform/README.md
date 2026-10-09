# UpdSpace ID: перенос в домашний k3s

Состояние на 2026-10-09: локальный ID работает на **пробной копии** данных.
Публичный `id.updspace.com`, облачная база и облачные задания остаются рабочим
источником. Переключения production и удаления облачных ресурсов не было.
VM: `updspace_m4tveevm@192.168.1.176`, node `updspace-home`.
Portal переносится отдельной задачей и владеет PostgreSQL.

## Размещение

| Компонент | Место | Состояние |
| --- | --- | --- |
| API, sessions, mutations, web, jobs, внутренний Caddy | namespace `updspace-id`, Deployment `id` | 6/6 Ready, закрытая пробная копия |
| Одна YDB для всего ID | `updspace-data/id-ydb`, database `/local`, TLS 2135 | 67 исходных таблиц восстановлены |
| Файлы ID и Portal | общий Garage в `updspace-data`, отдельные ключи и bucket | публичный HTTPS проверен |
| Пять фоновых расписаний ID | CronJobs в `updspace-id` | все `suspend: true` |
| Полный backup ID | VM → шифрование → рабочий компьютер | снят, расшифрован, пробно восстановлен |
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
диска и не заменяют отказоустойчивость. Перед публичным переключением нужно
явно принять этот риск либо подготовить поддерживаемое хранилище/другой backend.
Rescue boot, существующий k3s и посторонние workloads не менялись.

[YDB manifest](id/ydb.yaml) закрепляет image digest, requests 500m/1Gi и limits
2 CPU/4Gi, TLS, запрет анонимного доступа, UID 65534 и сетевую изоляцию.
[Конфигурация сервера](id/ydb-config.yaml) не содержит паролей. Runtime-пользователь
`idruntime` имеет connect/read/modify без DDL. Пароли root/idruntime, TLS CA и
ключи находятся только в Kubernetes Secrets и root-only каталоге
`/opt/updspace-data/id-ydb`. Поддельный `root@builtin`, неверный пароль,
анонимный доступ и DDL от runtime проверены и отвергаются.

## Точная версия приложения

Cloud production работает на commit `5bfa8dad20fcb2f34292bcd2481515df908ab2c9`.
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

Согласованный source snapshot `20261009T114911Z` содержит 67 таблиц и 1833 строки.
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
Перед финальным переносом вся пробная база должна быть заменена свежим снимком;
синтетические outbox-записи нельзя запускать в production.
Публичные JWKS и OIDC discovery облака и новой сборки полностью совпадают.
Настоящий изолированный Chromium с DNS override на VM также прошёл вход
с паролем/TOTP и личный кабинет, без отключения TLS, с Secure/HttpOnly cookie
и без page errors. Browser passkeys, реальные provider redirects и доставка настоящего письма ещё
не проверялись; синтетические HTTP проверки не подменяют их.

## Garage и общий edge

[Garage manifest](garage/garage.yaml): версия 2.4.1 закреплена digest, PV Retain
`/srv/updspace/garage`, S3 3900 доступен только из Caddy. RPC/admin — loopback.
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

Пользователь выбрал рабочий компьютер. Закрытый ключ шифрования находится только
в `~/.local/share/updspace-backups/keys`; на VM передан лишь публичный recipient.
Исходный cloud dump сохранён отдельно как `source-ydb.tar.gpg` и расшифрован с
проверкой SHA256. Полная локальная копия `20261009T122848Z/id.tar.gpg` содержит
sparse YDB disk/config, все ID runtime/DB Secrets, CA, шесть media objects,
Garage ID key и манифесты. Размер 128776068 байт.

[backup.py](id/backup.py) блокирует наложение, сохраняет состояние Deployment и
CronJobs, останавливает только ID writers/YDB, снимает холодный sparse archive и
логический ID-only S3 snapshot, возвращает исходное состояние, затем шифрует.
Пробная остановка заняла 18.01 s. При незавершённом resume остаётся
`/srv/backups/updspace-id/resume-state.json`: его нельзя удалять без проверки.
Завершённые ciphertext/manifest/COMMITTED лежат в
`/srv/backups/updspace-id-encrypted/<UTC stamp>`, незашифрованный stage закрыт root.

[Daily timer](id/updspace-id-backup.timer) подготовлен на 02:30 UTC (05:30 MSK),
**установлен на VM, пока не активирован**. Он означает короткое ежедневное окно недоступности ID;
длительность будет расти вместе с объёмом данных. [Offsite sync](id/sync-backups.py)
установлен как отдельный user timer на рабочем компьютере, работает каждый час,
проверяет COMMITTED и SHA256, защищён от одновременного запуска и нехватки места.
Новые копии автоматически не удаляются. При выключенном компьютере остаются
копии на VM; перенос возобновится при работе user service manager.

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

## Оставшееся переключение

1. Принять ограничение file-backed YDB или изменить целевое хранилище.
2. Origin уже проверен по HTTPS: отдельный сертификат Let's Encrypt до 2027-01-07,
   закрытый LAN-only stage route в общем Caddy. Публичный DNS остаётся в YC.
   Ручной DNS-01 сертификат сам не продлевается; при cutover управление TLS
   нужно передать Caddy ACME, до этого не считать renewals готовыми.
3. На короткое окно остановить облачные writers и пять timers; сохранить их
   прежние bindings/spec/status для отката. Снять свежие согласованные YDB/S3
   копии, доставить зашифрованную копию на рабочий компьютер и сверить restore.
4. Заменить пробную базу целиком, убрать `TRIAL_ONLY`, проверить готовность всех
   компонентов, включить ID route и сменить только ID DNS. Сохранить источник.
5. Проверить вход/внешние интеграции; включить локальные задания и daily backup.
   Автообновление TLS после переключения передать общему Caddy.

После первой записи на новой стороне простой DNS rollback теряет изменения:
нужна остановка writers и обратная синхронизация/восстановление. До первого
нового пользовательского write можно вернуть прежний cloud route/bindings.
