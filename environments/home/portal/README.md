# Portal на домашней VM

Публичный `https://portal.updspace.com` переведён на VM 2026-10-09 в 15:23:44 UTC.
Frontend и API работают в k3s; Cloudflare proxy сохранён. После приёмки переноса
старые восемь облачных контейнеров, gateway, outbox function и trigger выведены
из эксплуатации. Облачные YDB/S3, секреты и общий registry сохранены.
Ведомость очистки — [cloud-retirement-2026-10-09.md](../../../docs/cloud-retirement-2026-10-09.md).

Все дальнейшие изменения размещения находятся в этом каталоге центрального
`updspace/infra`. `HANDOFF.md` и `ROLES-HANDOFF.md` описывают ранние передачи;
их контрольные суммы исторические. Итоговое доказательство: `acceptance-production.json`.

## Размещение

VM `updspace_m4tveevm@192.168.1.176`, k3s `updspace-home`. Все восемь сервисов
Portal используют **одну PostgreSQL БД `updspace`**, отдельные роли и схемы
`portal_*`. БД не публикуется наружу. Общий PostgreSQL обслуживает также отдельную
GlitchTip БД; резервирование Portal не включает GlitchTip. ID перенесён своим
владельцем и сохраняет отдельную локальную YDB по решению пользователя.

| Облачный компонент | Локальная замена |
| --- | --- |
| Portal YDB | PostgreSQL 18.6, одна БД и восемь схем |
| Serverless Containers | Восемь Deployment в `updspace-portal` |
| Frontend Object Storage | Исходный bundle без пересборки, rootless nginx и read-only PV |
| Media Object Storage | Общий Garage, отдельный private Portal bucket/key |
| Gateway / TLS | Общий Caddy в `edge`, Cloudflare proxy остаётся включён |
| Lockbox runtime | Kubernetes Secrets; защищённые исходные значения root-only на VM |
| Timer Function | `portal-outbox` каждые 15 минут; остальные 13 CronJobs приостановлены |
| Cloud image registry | Локальный OCI archive и `imagePullPolicy: Never` для backend |

Namespace `updspace-data` общий: не удалять его и не применять общую deny policy.
Общий Caddy и Cloudflare меняются только после передачи владения общей конфигурацией.
Итоговый Portal fragment — `Caddyfile.production.fragment`; полный Caddy ведёт
владелец edge. При Portal cutover остальные блоки, включая ID, сохранены побайтово.

## Проверенный результат

- Финальный snapshot `20261009T150956Z-492adaae`: 78 таблиц и 371 исходная строка.
  Все хеши совпали с контрольным frozen snapshot `20261009T145451Z-51f75ac4`.
  Перед финальным снимком выдержаны 990 секунд: завершение запросов и 900s media PUT TTL.
- Импорт проверил полное покрытие таблиц, нормализованные SHA256, FK, владельцев
  схем и последовательности. Общие пустые framework tables распределены по схемам;
  `django_migrations` созданы штатными миграциями точных исходных приложений.
- Финальный backup `20261009T152137Z-yfOpRh` передан зашифрованным на workstation,
  расшифрован только там и восстановлен в отдельный временный PostgreSQL:
  78 таблиц / 371 строка / 9 владельцев / 73 последовательности совпали.
- Оба медиафайла, 992182 байта, перенесены с metadata. Offsite backup
  `20261009T152139Z-aa4a745f` восстановлен в временный префикс Garage;
  SHA256 и metadata совпали, тестовые объекты удалены.
- Frontend: 3152 файла, 44219388 байт, проверены source ETag и локальные SHA256.
  Release `/srv/updspace/portal-frontend/releases/20261009T134201Z-cfa3a145`.
- Все девять Deployment готовы. Ручной запуск `portal-outbox` завершился успешно.
- Публичные проверки из домашней сети и облачной VM: главная 200, `/api/v1/csrf`
  200, `/api/v1/session/me` без входа 401; API header `X-Portal-Backend: vm`.
  JS bundle 950639 байт совпал по SHA256. Chromium: три ресурса загружены,
  ошибок страницы и сетевых запросов нет. Переход входа открывает ID `/login` (200).
- Полный SSO password/MFA/consent/callback и 22 capabilities проверены на stage
  синтетической учётной записью. Тогда же проверены чтение шести сервисов,
  PUT/read/restore preferences, tenant isolation 403/409 и сохранение сессии
  после рестарта BFF. Финальный импорт удалил Portal fixtures; реальный
  пользовательский вход после cutover не выполнялся.

ID issuer/client/callback и исходные Django/HMAC/OIDC/media encryption keys
сохранены. Runtime использует `https://id.updspace.com/api/v1`. Временные
`bff-to-id-stage` и `id-from-portal-stage` policies удалены, как и inspect pods.

## Образы без облачного ключа

`portal-images.json` хранит исходные cloud digest. `portal-local-images.json`
содержит используемые локальные digest. `localize-images.py` добавляет только
метку `com.updspace.source-image`: проверяет SHA256 всех исходных blobs,
сохраняет каждый слой и остальную runtime configuration. Код и зависимости
приложений не обновлялись. Изменение метки создаёт новый config/manifest digest.

Это нужно для k3s 1.36: ранее скачанный приватный image сохраняет запись о
проверке registry credential даже при `Never`. Локально импортированные образы
с новой identity штатно разрешены политикой `NeverVerifyPreloadedImages`.
Политика безопасности кластера не менялась. `source-registry` удалён, ссылок
`imagePullSecrets` нет, получение IAM token для запуска Portal не требуется.

Архивы на VM в `/srv/updspace/backups/portal-images/20261009T141656Z/`:

- `images.oci.tar`: исходные восемь backend и nginx, `SHA256SUMS`;
- `portal-local.oci.tar`: локальные backend, `portal-local.SHA256SUMS`.

При восстановлении проверить оба SHA256, импортировать оба архива через
`sudo k3s ctr -n k8s.io images import --platform linux/amd64 <archive>` до запуска
приложений. Nginx использует `IfNotPresent` и также содержится в исходном архиве.
Архивы образов находятся на VM. Дополнительный зашифрованный recovery bundle
`/srv/updspace/backups/portal-recovery/20261009T162957Z/recovery.tar.gpg` содержит
оба OCI archive, 3152 файла frontend, восемь runtime Secrets, восемь Portal DB roles,
Garage Portal key, frozen export и конфигурацию. Временный IAM token и credentials
других компонентов исключены. Bundle на рабочий ПК не передавался: владелец
отказался от хранения архивов на ПК. Это копия на той же VM, не offsite.

## Применение и защищённые входы

`render-active.py` читает `rollout.json`, `render-portal.py`,
`portal-local-images.json` и `frontend.json`. Обычный render означает production:
девять Deployment и только `portal-outbox`; `--dormant` выключает все приложения
и задания для восстановления данных. `prepare-runtime.py --apply` обновляет
восемь runtime Secrets; после изменения Secret перезапустить соответствующие
Deployment. Без `--apply` скрипт только валидирует входы.

Секретные входы не входят в Git и остаются root-only:

- `/opt/updspace-portal-migration/source-runtime.json`;
- `/opt/updspace-data/credentials.json`;
- `/opt/updspace-data/garage/portal-credentials.json`;
- `/opt/updspace-data/backup-recipient.asc` — только публичный GPG recipient.

`export-ydb.py` использует существующий YC CLI и SDK 3.28, передаёт данные
через SSH непосредственно на VM. Для нового согласованного snapshot остановить
всех writers: чтение отдельных таблиц не является общей транзакцией.
`import-trial.py` требует все приложения выключенными и подготовленные inspect
pods. Он проверяет COMMITTED/SHA256/model coverage, затем импортирует схемы.
Все восемь схем не объединены одной транзакцией: включать приложения только
после успеха всех импортов. Не запускать импорт поверх работающего production.

## Backup и восстановление

VM backup timer остаётся включённым: ежедневно 03:15 UTC + до 300s.
Workstation offsite timer `updspace-postgres-offsite.timer` disabled/inactive
по решению владельца; прежний каталог `~/.local/share/updspace-backups/portal-postgres`
с архивами удалён. Закрытый GPG key сохранён в `~/.local/share/updspace-backups/keys`: его
нельзя удалять вместе с архивами. Исторические offsite restore proofs выше остаются
доказательствами проверки переноса, но актуальной внешней копии Portal сейчас нет.
`COMMITTED` на VM ставится после проверки SHA256. Автоматического удаления копий нет.

PG backup включает только `updspace`; roles dump без паролей. Для полного
восстановления требуются protected runtime inputs. `restore-verify.py`
восстанавливает расшифрованную копию в изолированный временный PostgreSQL и
сравнивает с исходным snapshot; после появления новых записей выбирать snapshot
или критерии проверки, соответствующие дате копии.

`backup-media.py` проверяет инвентарь до/после, bytes/SHA/metadata; plaintext
только в памяти, затем GPG. Лимит 64 MiB, при росте заменить на потоковый backup.
Media читается непосредственно из Garage до модификации HTTP headers Caddy.
Узкая `garage-backup-network.json` разрешает только Activity Portal доступ к Garage.

## Откат и оставшиеся ограничения

Облачная конфигурация до заморозки сохранена root-only в
`/opt/updspace-portal-migration/cloud-freeze/`: Gateway spec, trigger state,
исходный DNS и freeze proof. Cloud outbox trigger `a1s30tucfpcid4tsgd2q` и
Cloud Gateway `d5da1fs5arjv790l1uua` удалены при согласованном сокращении облака.
**После новых локальных записей простой возврат DNS потеряет эти изменения.**
Сначала остановить локальных writers, сохранить свежий backup и вернуть delta
в источник; runtime/gateway/trigger потребуется создать заново из сохранённой
конфигурации с новыми resource IDs, затем согласованно восстановить DNS.
Облачные данные не удалять до отдельного решения владельца.

Portal origin certificate действителен до 2027-01-07 и пока подключён явно
через `edge/portal-origin-tls`; автоматическое продление Portal не настроено.
Cloudflare обновляет посетительский сертификат отдельно. ID уже использует
свою автоматическую ACME-конфигурацию; его настройки здесь не менять.

В закреплённом исходном Events image есть существующая несогласованность путей:
`/api/v1/events` даёт upstream 404, `/api/v1/events/events/` работает. Исправление
прикладного API не входит в перенос. Аппаратная надёжность VM не сертифицирована
этой миграцией; rescue boot и существующие данные хоста не изменялись.

## Проверки исходников

Из корня центрального репозитория:
`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s environments/home/portal -p 'test_*.py'`.
Server schema check: render-active → `kubectl apply --dry-run=server -f -`;
отдельно `portal-network.yaml`.
