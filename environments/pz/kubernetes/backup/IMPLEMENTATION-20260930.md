# Внедрение backup: 30 сентября 2026

**Внедрение продолжается: первый backup запущен; перенос и расписание ожидают restore drill.**
Владелец разрешил выпуск трёх постоянных S3-ключей, их хранение 0600 в локальном
`operator-backups`, остановки сейчас, ежедневный backup в 06:00 МСК и Monium.
Ключ age создан вне VM; на VM доставлен только public recipient. Restore
credentials и приватная age identity остаются на рабочей машине.

## Что применено

Цель: `compute-vm-2-6-60-ssd-1785610759198` (`51.250.40.253`, `ru-central1-d`).
Игра и панель при подготовке не останавливались и не перезапускались.

| Ресурс | Фактическое состояние |
| --- | --- |
| SSD данных | 40 GiB `network-ssd`, ID `fv4m5c8adoo0lqn97bap`; подключён, ext4 `33e9e5a5-a8c4-46cd-a745-bc2043c9237a`; подготовлен в `/srv/pz-data-migration`, игровых данных на нём пока нет |
| HDD spool | 64 GiB `network-hdd`, ID `fv4hijsh39cf6odaj1i5`; ext4 `d6538e50-3553-4072-8b87-be5687764f0d`, смонтирован `/srv/pz-backup-spool`, UUID сохранён в fstab |
| Bucket | приватный `upds-pz-backup-b1gidr45ifb2c25bco2d`, COLD, prefix `pz/production/`, capacity guard 100 GiB; versioning/Object Lock не включены |
| IAM | отдельные uploader/retainer/restore accounts, bucket-scoped baseline roles и ограничивающая policy; три постоянных ключа выпущены отдельно от Terraform и Git |
| Backup runtime | `/opt/pz-backup`, отдельный SDK `/opt/pz-backup-venv`; age 1.1.1, boto3 1.43.105 |
| Служебные Unix accounts | `pz-backup-upload`, `pz-backup-retain`; общий lock и раздельные закрытые state/credentials paths |
| systemd | coordinator/upload разрешены для первого запуска; расписание и retention пока выключены |
| Существующие данные | по-прежнему `/var/lib/pz-volumes/zomboid.ext4`, UUID `0bd0ee81-1d96-447e-868a-405226663cf6`, mount `/srv/pz-storage/zomboid` |

SSD 50 GiB первоначально не создался из-за квоты: свободно 40 GiB из 200 GiB.
Выбран 40 GiB; квота не изменялась, чужие ресурсы не удалялись. Все данные
занимали около 21,4 GiB; в том числе предыдущие startup ZIP — около 8,1 GiB,
Saves — около 2,1 GiB. Полный восстановимый набор также включает binaries,
моды, панель, Steam и конфигурации, поэтому 5 GiB общего S3 — не гарантированный
размер. Его покажет первая реальная упаковка. Инвентаризация без чтения содержимого дала
584752 записи и 12461415467 байт (11,61 GiB) после двух заявленных исключений.
Расчёт staging + худший архив до OCI/reserve — 29996122150 байт (27,94 GiB).
Инвентаризация заняла 113 секунд при CPU quota 50%, peak RSS около 384 MiB.

По проверенному [тарифу Compute Cloud](https://yandex.cloud/ru/docs/compute/pricing#prices)
диски стоят **до 820,78 ₽ за 31 день**: SSD 592,22 ₽ + HDD 228,56 ₽.
По [тарифу Object Storage](https://yandex.cloud/ru/docs/storage/pricing#prices)
COLD стоит 0,00176 ₽/GiB·час; например, пять архивов по 5 GiB — около 31,68 ₽
за 30 дней, дополнительно операции/платный внешний трафик при превышении квот.
Диски оплачиваются уже с момента создания. Расчёт не включает существующую VM.

## Проверки

- Terraform `cloud/` и `backup-cloud/` после apply: все managed resources `no-op`.
- Новые диски сверены по cloud ID, serial, размеру и отсутствию signatures перед mkfs.
- Инициализация дисков, установка зависимостей и installer: systemd exit 0.
- На PZ VM прошли **120 backup tests**, включая настоящий age → zstd → tar
  roundtrip с временным тестовым ключом и синтетическими данными.
- Дополнительно локально прошли 20 deploy, 22 storage, 9 growth tests;
  Terraform backup-cloud — 3 mocked tests.
- Первый `pz-backup.service` запущен вручную после положительной IAM-проверки.
  Ежедневный timer и retention ещё не включены; требуется полный restore drill.
- Подготовлены CI и collector-конфигурация для метрик backup; rollout collector,
  правила/уведомления Monium и их сквозная проверка ещё не выполнены.
- Live S3 IAM-canary: 26 endpoint-проверок и 41 policy-fixture проверка прошли;
  все временные объекты и multipart убраны. У трёх runtime accounts нет прямых
  folder/cloud IAM grants. GET отсутствующего объекта вне префикса подтвердил
  отсутствие данных; запрет чтения существующего объекта проверен fixture,
  не выдаётся за live AccessDenied. Полный restore drill ещё не выполнен.

## Отказы и их обработка

Первый apply создал bucket/spool/accounts, но отказал на SSD quota и S3
PutBucketVersioning. Ресурсы перечитаны, state сохранён; существующий bucket не
пересоздавался. Для нового unversioned bucket убран лишний versioning PUT.

Автоматическая проверка отклонила широкое `s3:*` для provisioner. Вместо него
применён перечень конкретных bucket metadata GET/LIST и PutLifecycleConfiguration;
DeleteBucket, запись объектов и изменение policy/ACL этим Allow не выдаются.
Terraform смог перечитать bucket и закончить настройку. Это разрешение относится
к ранее существовавшей управляющей учётной записи, не к uploader/retainer/restore.

Отдельная автоматическая проверка заблокировала создание трёх постоянных S3 keys
и сохранение их секретов в локальных файлах без явного согласия владельца.
Обходов и временных production credentials не использовалось. После явного
разрешения владельца ключи выпущены, локальные файлы имеют права 0600.

Первая canary выявила особенности Yandex: обязательный If-None-Match нужен
также при multipart initiation/parts, а ListBucketMultipartUploads не получает
s3:prefix context. SDK подписывает этот заголовок; policy разрешает только
перечень multipart metadata в отдельном bucket. Запрет unconditional PUT и
перезаписи сохранён. Повторная проверка и точная очистка прошли.

## Порядок завершения внедрения

1. Выпустить раздельные S3 credentials в закрытые локальные файлы; создать age
   identity вне игровой VM, передать на VM только public recipient. Restore
   credentials/private identity сохранить вне игровой VM и отдельно архивировать.
2. Выполнить `check-iam.py` на настоящем endpoint, включая conditional multipart
   и отрицательные права; проверить точную очистку canary объектов.
3. Настроить coordinator по фактическим UUID/размерам, выполнить первый backup,
   убедиться в исходном состоянии приложений и в remote COMMITTED/readback.
4. Скачать копию read-only account, проверить SHA/извлечение и запустить точный
   восстановленный мир в изоляции. Измерить RTO, только затем создать аттестацию.
5. Через reviewed disk helper перенести игру на SSD с проверкой SHA/metadata,
   переключить UUID mount и k3s boot guard. Старый loop сохранить; не запускать
   старые storage/growth provisioners для «повторной настройки».
6. Включить согласованное расписание, Monium alerts и retention после drill.
   Проверить реальное накопление пяти комплектов; не создавать искусственно
   пять одинаковых снимков для отчёта.

Приватные state/планы/отчёты расположены в
`environments/pz/private/backup-implementation/` и не включаются в Git.
Подключения закреплены в приватном `cloud/backup-disks.auto.tfvars.json`;
не удалять его перед обычным cloud plan, иначе будет предложено отсоединение дисков.
`backup-cloud` использует отдельный local backend с файловой блокировкой.
Базовая миграция репозитория находится в `origin/chore/pz-k3s-iac`, commit `0cc5774`.
Открыты draft PR [базовой миграции #12](https://github.com/updatingspace/infra/pull/12)
и [cloud-ресурсов #13](https://github.com/updatingspace/infra/pull/13); CI #13 прошёл.
Runtime готовится отдельным зависимым PR. Merge и автоматический production
deployment не выполнялись.
