# Проверяемые резервные копии PZ

Реализация [плана](../BACKUP-ROADMAP.md). Устройство и обязательные проверки описаны
в [обзоре](IMPLEMENTATION-20260930.md); фактический статус хранится в приватном
отчёте оператора. Наличие кода не означает успешный backup.

## Формат и процессы

`coordinator.py` запускается доверенным root helper через systemd. Он проверяет
UUID обеих ext4, свободные блоки/inode и бюджет staging + худший размер архива,
берёт общий `/var/lib/pz-volumes/migration.lock`, приостанавливает updater,
дожидается терминальных Jobs/Pods/journal и берёт его существующий lock. Игра и
панель останавливаются через replicas=0; для игры нужны событие exit 0,
подтверждение runtime и отсутствие процессов в исходной cgroup. Неизвестное
завершение запрещает согласованную копию и требует разбора.

В staging входят полный набор данных (`pz-server`, `zomboid`, `steam`, `panel`,
`panel-logs`), числовые владельцы/права, ссылки, xattrs/ACL, точные OCI образы,
Kubernetes Secrets/ConfigMaps/workloads и исходники восстановления. `gather-recovery.py`
проверяет хэши OCI blobs и связь работающего image digest с экспортом; отдельный
index фиксирует PZ build и Workshop/mod IDs. Terraform states и старый архив
миграции сохраняются отдельно от пяти игровых снимков.

Явные исключения ограничены предыдущими архивами: `zomboid/backups` и
`panel/.k8s-panel-updater/backups`. Их список записывается в зашифрованный manifest.
Сохранения, БД и конфигурации остаются в снимке. Новые исключения требуют отдельной
проверки полноты и изменения allowlist; логи/cache автоматически не исключаются.

Содержимое проходит два чтения: SHA256 исходного файла считается при записи
тех же байтов в staging TAR, затем после fsync весь TAR независимо перечитывается
с диска. Проверяются каждый SHA256, точный состав, metadata и manifest, который
записывается последним после вычисления всех хэшей. Замена исходного inode,
изменение файла или его метаданных приводят к отказу; повторный inventory
источника обнаруживает добавленные или удалённые записи. Обе проверки выполняются
при остановленных писателях в пределах исходного лимита времени.

После проверки источника и staging возвращаются исходные
replicas/updater. Если игру заранее остановили, backup её не запускает. Проверенный
tar поток сжимается zstd и шифруется age; VM достаточно публичного recipient.
Шифротекст и зашифрованный manifest попадают в `<id>.ready`. Uploader получает
только чтение этих файлов, отдельные S3 credentials и отдельный Unix account.
Он загружает оба объекта с conditional writes, полностью перечитывает их и лишь
последним публикует и перечитывает `COMMITTED.json`.

`cleanup.py` удаляет только собственный `<id>.ready` после независимого полного
GET/readback от непривилегированного helper. Проверяются корневая конфигурация
bucket/endpoint/prefix, receipt, локальные SHA и commit. Прерванное удаление
продолжается по журналу; `.partial`, активные данные и ручные backups не затрагиваются.

`remote.py retain` имеет отдельные credentials и сериализуется с uploader.
Очистка выключена до успешного полного restore drill. Сохраняются пять новейших
проверенных комплектов по времени снимка и ID. Неполный listing, повреждённые
объекты, незавершённые snapshots или неоднозначное состояние блокируют удаления.
Ошибка оставляет лишние комплекты. Lifecycle очищает только незавершённый multipart.

## Установка и запуск

Нужны `age`, `zstd`, GNU `tar`, `rsync`, `k3s`, Python3 и отдельное SDK окружение
`/opt/pz-backup-venv`. Версия boto3 закреплена в `requirements.txt`;
проверяется поддержка IfNoneMatch у PutObject и CompleteMultipartUpload.

`install.py --source /root/reviewed-bundle --config /root/reviewed-settings.json`
устанавливает только перечисленные исходники и systemd units, создаёт две
служебные учётные записи и закрытые каталоги. Никакие сервисы или timers автоматически
не включаются. Существующее активное расписание блокирует переустановку; изменение
конфигурации требует её прежнего SHA256. Credentials доставляются отдельно,
не через Terraform, Git, stdout или CI artifacts.

Конфигурация coordinator: фиксированные data_root/spool, data_uuid/spool_uuid,
age_recipient, infra_revision, server_name, min_free_bytes/min_free_inodes,
max_snapshot_bytes/max_entries, stop_timeout_seconds, recovery_helper и явные
excluded_paths. `uploader_gid` определяет installer. RemoteConfig фиксирует bucket,
prefix, endpoint, регион, storage_class и общий remote lock. Разные файлы credentials
нужны uploader, retainer и внешнему restore-оператору.

Перед первым запуском выполнить `coordinator.py inventory` и `check-iam.py` с
тремя учётными записями и фактической policy. Canary не вызывает DeleteBucket или
PutBucketPolicy: административные отрицательные права проверяет fixture, остальные
операции — настоящие disposable objects с точной очисткой и журналом.

Первый запуск `pz-backup.service` разрешается отдельным `/etc/pz-backup/enabled`.
Для загрузки нужен `upload-enabled`; для очистки старых удалённых комплектов —
`retention-enabled` и аттестация успешного восстановления. Пустой timer не задаёт
расписания: reviewed OnCalendar drop-in добавляется после первого restore drill.
Публичный ключ остаётся на VM, приватный ключ расшифрования хранится отдельно.

## Восстановление

1. `remote.py restore` под read-only account скачивает точный committed комплект
   и проверяет размеры/SHA. Указать новый отдельный каталог загрузки.
2. `restore.py --download-dir ... --identity /external/age.key --destination NEW_DIR`
   расшифровывает в новый изолированный каталог, проверяет каждую запись и её metadata.
   Выход за каталог, специальные файлы, опасные ссылки и лишние/отсутствующие файлы
   приводят к отказу. Ключ не передавать аргументом-значением и не копировать на PZ VM.
3. Запустить восстановленный точный образ/мир без production volumes, внешних портов
   и production-интеграций. Проверить RCON и загрузку существующего мира, записать RTO.
4. Только фактический успешный runtime drill позволяет создать
   `pz-backup-restore-attestation-v1`, привязанный к bucket/endpoint/prefix,
   snapshot format/ID и SHA commit. `restore.py` сам эту аттестацию не создаёт.

Для нового полного извлечения на Linux ≥5.8 можно явно передать `restore.py`
параметр `--durability filesystem`. По умолчанию остаётся `per-file`: отдельный
fsync каждого файла и каталога. Новый режим открывает FD доверенного parent
каталога **до mkdir и первой записи**, проверяет совпадение filesystem и держит
FD до окончания операции. После всех SHA/metadata-проверок и сохранения manifest
выполняется один проверяемый `syncfs`; fsync manifest и итогового отчёта остаются.
Parent должен принадлежать root или текущему UID и не допускать записи группы
или остальных пользователей. Отчёт содержит `durability=filesystem-syncfs`.

Полный age/zstd поток, inventory и повторная проверка извлечённого дерева
сохраняются; `restored.json` появляется только после успешных exit codes обоих
процессов. Любая ошибка барьера, включая EIO/ENOSPC/EDQUOT, завершает попытку
без retry и без success report. Даже ошибка другой записи на том же filesystem
может вызвать отказ. Повторный syncfs не используется для отмены уже полученной
ошибки: kernel продвигает error cursor открытого FD. Семантика описана в
[sync(2)](https://man7.org/linux/man-pages/man2/sync.2.html),
[open.c](https://github.com/torvalds/linux/blob/v6.8/fs/open.c) и
[sync.c](https://github.com/torvalds/linux/blob/v6.8/fs/sync.c).
Этот режим не разрешает reuse/очистку прежнего trial: destination остаётся NEW,
а failed reports и исходная временная шкала RTO сохраняются отдельно.

`runtime-drill.py` проверяет изолированный запуск точного сохранённого образа,
RCON, чтение существующего мира и штатное завершение. Workshop providers
подключаются только из восстановленного дерева через проверяемый список bind
mounts. Явный `-cachedir=/zomboid` сохраняет те же saves/config paths и исключает
расхождение canonical/lexical путей загрузчика при симлинках. Если production уже имеет
предупреждение об отсутствующем моде или несколько поставщиков одного mod ID,
можно передать `--mod-baseline` с приватным отчётом, снятым **до** snapshot.
Он должен подтверждать фактически загруженные IDs, выбранные каталоги,
хэши конфигурации и загрузчика. Новые предупреждения или несовпадение этой
базы запрещены. Без baseline проверка отказывает при отсутствии или неоднозначности
мода. Адаптация Workshop для offline-запуска затрагивает только отдельную
восстановленную копию и записывается в отчёт drill.

Для переноса игрового диска использовать [DISK-MIGRATION.md](DISK-MIGRATION.md).
Старый loop не удаляется, migration и storage-growth нельзя запускать повторно.
При несовпадении UUID k3s boot guard запрещает запись в пустой каталог root.
Перед копированием миграция проверяет read-only superblock ext4 и снимает SHA256
всех исходных файлов. После копирования повторно сверяются все метаданные источника
и полные SHA256/метаданные назначения; read-only проверяется до и после сверки.
Это убирает повторное чтение содержимого неизменяемого источника, включая старые ZIP,
но не исключает их из копии и не сокращает проверку нового диска.

## Наблюдаемость и проверки

`metrics.py` отдаёт на private node IP:9109 возраст последнего commit, длительность
upload/простоя, размер, незавершённые операции и свободное место/inode. Collector
передаёт их в Monium. Внешний доступ к 9109 не открывается. Расписание, RPO и канал
alerts — операторские настройки; отсутствие нового commit не маскируется успешным
перезапуском игры.

Проверки без production credentials:

```sh
python3 -m unittest discover -s environments/pz/kubernetes/backup -p 'test_*.py'
python3 -m unittest discover -s environments/pz/kubernetes -p 'test_deploy*.py'
terraform -chdir=environments/pz/kubernetes/backup-cloud test
```

CI проверяет эти сценарии и исключает state/plan/credentials из Git. CI не выполняет
production apply и не получает облачные credentials.
