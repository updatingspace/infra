# Portal на домашней VM

Актуальное состояние на 2026-10-09: **закрытый локальный stage готов; публичный
Portal и единственный активный облачный outbox timer ещё работают в облаке**.
Все дальнейшие изменения размещения находятся в этом каталоге центрального
`updspace/infra`. `HANDOFF.md` и `ROLES-HANDOFF.md` описывают более ранние передачи,
их контрольные суммы являются историческими, а не проверкой текущего каталога.

## Размещение

VM `updspace_m4tveevm@192.168.1.176`, k3s `updspace-home`. Все восемь приложений
Portal используют **одну PostgreSQL БД `updspace`**, разные роли/схемы. Общий
PostgreSQL обслуживает также отдельную GlitchTip БД; резервирование Portal не
включает GlitchTip. ID сохраняет отдельную локальную YDB по выбору пользователя;
его публичный перенос ведётся независимо.

| Облачный компонент | Замена |
| --- | --- |
| Portal YDB | PostgreSQL 18.6, восемь схем `portal_*` |
| Serverless Containers | Восемь Deployment в `updspace-portal`, точные исходные images по digest |
| Frontend Object Storage | Исходный bundle без пересборки; rootless nginx, read-only PV |
| Media Object Storage | Общий Garage, отдельный private Portal bucket/key |
| Gateway / TLS | Общий Caddy в `edge`; отдельный сертификат `portal.updspace.com` |
| Lockbox runtime | Существующие значения в Kubernetes Secrets, исходники root-only на VM |
| Timer Functions | CronJobs; сейчас все suspended, при cutover включить только `portal-outbox` раз в 15 минут |

Общий Caddy, Cloudflare и платформу наблюдаемости меняет владелец общей
инфраструктуры. Namespace `updspace-data` общий: не удалять его и не применять
общую deny policy. PostgreSQL не публикуется наружу. Private TLS key остаётся VM;
широкий wildcard key из облака не экспортировался.

## Текущее доказательство переноса

- Пробный YDB snapshot `20261009T133350Z-4d036782`: все 78 таблиц, 371 строка.
  После импорта совпали полные нормализованные SHA256, FK и schema owners.
- Все восемь точных образов прошли `makemigrations --check --dry-run` и штатные
  PostgreSQL migrations. Исходные общие framework tables пусты и проверены до
  распределения по схемам; `django_migrations` создан PostgreSQL migrations.
- Backup `20261009T135805Z-Pdv85P` зашифрован и передан на рабочий компьютер.
  Он расшифрован только там и восстановлен в отдельный временный PostgreSQL:
  78 таблиц / 371 строка / 9 владельцев / 73 последовательности проверены.
- Два media objects, 992182 байта, скопированы с metadata и проверены SHA256.
  Logical media snapshot `20261009T142036Z-88b72d82` зашифрован на VM. Его
  offsite restore пока не завершён; не считать его полным доказательством restore.
- Frontend: 3152 файла, 44219388 байт; MD5 source ETag и SHA256 локальных файлов
  проверены. Текущий release `/srv/updspace/portal-frontend/releases/20261009T134201Z-cfa3a145`.
- OCI backup всех девяти образов:
  `/srv/updspace/backups/portal-images/20261009T141656Z/images.oci.tar` (657 MiB).
  Изолированный import containerd прошёл; runtime использует `IfNotPresent`.
- Chromium SSO: Portal → ID password → MFA → consent → callback → `/t/aef`,
  TLS проверен, session/me 200, 22 capabilities, ошибок страницы нет.
- Через BFF проверены communities, voting, events, feed, gamification,
  preferences; PUT/read/restore preferences прошёл. Чужой tenant: 403
  `TENANT_FORBIDDEN`, несовпадающий expected tenant: 409 `TENANT_CONTEXT_CHANGED`.
  Сессия и preferences пережили перезапуск BFF.

При проверке точного исходного образа Events обнаружено: `/api/v1/events`
возвращает upstream 404, а `/api/v1/events/events/` работает. Это существующая
несогласованность BFF и Events paths в закреплённых образах; здесь код приложений
не меняется. Отдельный `/portal/me` не является рабочим frontend endpoint;
свой профиль входит в `/session/me`. Обычному участнику закрыты admin flags и
список чужих профилей (403).

Синтетические stage fixtures находятся только в локальной копии и будут удалены
финальным импортом. ID secrets/issuer/client/callback сохранены. Временный
`prepare-runtime.py --id-stage` использовался только для SSO; штатный runtime
смотрит на `https://id.updspace.com/api/v1`. Перед cutover проверить отсутствие
`bff-to-id-stage` и `id-from-portal-stage` policies.

## Исходники и защищённые входы

`render-active.py` читает `rollout.json`, базовый `render-portal.py`,
`portal-images.json` и `frontend.json`. `--dormant` останавливает все приложения
и CronJobs на время импорта. Обычный render сейчас означает stage (9 Deployment,
14 suspended CronJobs), **не публичный cutover**.

`prepare-runtime.py` запускается от root на VM, по умолчанию только валидирует;
`--apply` обновляет Secrets, сохраняя исходные Django/HMAC/OIDC/media encryption
keys. При изменении Secret отдельно перезапустить соответствующий Deployment.
Секретные входы не входят в Git:

- `/opt/updspace-portal-migration/source-runtime.json`;
- `/opt/updspace-data/credentials.json`;
- `/opt/updspace-data/garage/portal-credentials.json`;
- `/opt/updspace-data/backup-recipient.asc` (только публичный GPG recipient на VM).

Временные `inspect-<service>` pods созданы из точных образов, не выбираются
Service и живут 7200 секунд. `import-trial.py` требует их доступности и нулевого
числа application replicas. После работы их удалить. При новом запуске сначала
проверить protected manifest
`/opt/updspace-portal-migration/tools/portal-inspection.json`; это не постоянный
workload. Экспорт и приватные proofs лежат в root-only migration directory VM.

`export-ydb.py` запускается на workstation с установленным YDB SDK 3.28 и
существующим YC CLI. Получает временный IAM token, читает таблицы, передаёт
данные через SSH непосредственно на VM. Снимки отдельные по таблицам: для
финальной согласованности обязательно остановить всех облачных writers.

`import-trial.py <snapshot-directory>` проверяет COMMITTED и SHA256, model
coverage и отсутствие запущенных apps, затем вызывает `import-ydb-service.py`
в каждом inspect pod. Внутри каждой схемы замена атомарна; все восемь схем не
объединены одной транзакцией, поэтому приложения оставлять выключенными до
успеха всех импортов. `restore-verify.py <decrypted-backup-dir> <snapshot.json>`
восстанавливает backup в изолированную временную БД и сравнивает исходные строки.

## Backup и восстановление

PostgreSQL timer VM: ежедневно 03:15 UTC + до 300s. Workstation offsite timer:
ежечасно + до 300s. Каталог workstation:
`~/.local/share/updspace-backups/portal-postgres`. Только ciphertext передаётся
постоянно; закрытый GPG key остаётся `~/.local/share/updspace-backups/keys`.
`COMMITTED` ставится после проверки SHA256. Автоматического удаления копий нет.
Roles dump не содержит паролей; для полного восстановления нужны protected
runtime inputs. Cloud source пока сохраняется для отката.

`backup-media.py` создаёт отдельный logical snapshot Garage Portal bucket:
инвентарь до/после должен совпасть, проверяются bytes/SHA/metadata, plaintext
только в памяти, далее GPG. Ограничение 256 MiB; при росте заменить на потоковое
копирование. `sync-media-backups.py` повторно использует SHA-verified transport
и пишет ciphertext в `portal-postgres/media`. Media timer ещё не установлен.

OCI restore: проверить `SHA256SUMS`, затем на восстановленной VM
`sudo k3s ctr -n k8s.io images import --platform linux/amd64 images.oci.tar`.
Архив содержит исходные digest references; отдельный платный registry не нужен.

## Дальнейшее переключение

1. Проверить новый публичный origin с сохранённым Cloudflare proxy и текущим
   API в облаке. Кандидат `Caddyfile.edge-first.fragment` передаётся владельцу
   edge, самостоятельно не применяется. При регрессии больших assets вернуть
   исходный DNS: на этом этапе все записи остаются облачными.
2. Сохранить свежие Gateway spec, trigger state, image/revision inventory.
   Убедиться, что только Portal outbox timer активен и контейнеры не публичны.
3. Остановить новые обращения cloud API Gateway maintenance-response и pause
   trigger `a1s30tucfpcid4tsgd2q`. Container timeout 60s, медиа presigned PUT
   действует 900s: выдержать не менее 15 минут после последней выдачи, затем
   убедиться в неизменности данных и media. ID trigger не трогать.
4. Dormant render, финальный YDB export/import, сверка всех строк/хешей,
   финальное media reconcile, новый PostgreSQL backup/offsite/isolated restore.
   Если target media уже отличается, copy-media отказывает в overwrite —
   выяснить источник различия, а не удалять автоматически.
5. Вернуть штатный public ID runtime, включить приложения, проверить API.
   Только затем edge owner меняет API route на локальный BFF и включает
   `portal-outbox` раз в 15 минут. Другие 13 jobs остаются suspended.
6. Проверить публичные HTTPS/assets/auth/tenant/media и выполнение outbox.
   Cloud оставить frozen. До первых локальных записей rollback обратим возвратом
   Gateway spec/trigger/DNS; после них сначала сверить и вернуть delta, иначе
   простой DNS rollback потеряет новые данные.

## Проверки исходников

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s environments/home/portal -p 'test_*.py'`
из корня центрального репозитория. Schema dry run: render-active →
`kubectl apply --dry-run=server -f -`; отдельно `portal-network.yaml`.
Proofs stage не доказывают финальное переключение или сохранение новых записей
после последнего trial snapshot.
