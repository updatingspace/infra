# Backup cloud resources

После переноса PZ на локальный k3s этот root управляет только приватным S3
bucket, тремя service account и их bucket IAM bindings. S3 и его данные
сохраняются. VM, PV, mounts и игровые данные этот root не меняет.

Игровой SSD 40 GiB и HDD spool 64 GiB выведены из конфигурации 9 октября 2026.
Блоки `removed` с `destroy = false` не удаляют диски сами и не создают их заново:
оператор отдельно отключает и удаляет их после проверки переноса. В `cloud/`
должен оставаться пустой `backup_disks`. Работающая helper VM и её boot disk
сохраняются для Minecraft. Bucket защищён `prevent_destroy` и
`force_destroy = false`.

## Хранение резервных копий

Выбран `COLD`, не `ICE`. По таблице
[Object Storage](https://yandex.cloud/ru/docs/storage/pricing#prices), COLD:
0,00176 ₽/GiB·час; GET/HEAD 1,15 ₽/10000, PUT/POST/LIST 1,39 ₽/1000;
DELETE бесплатно. 5 GiB суммарно стоят около 6,34 ₽ за 30 суток;
пять архивов по 5 GiB — около 31,68 ₽. Размеры — разные сценарии, фактический
объём подтверждает первый архив; настройки, mods, build и зашифрованные secrets
тоже занимают место. Временно хранится шестой набор, незавершённые multipart и
отдельные архивы версий. Между сервисами Yandex трафик бесплатен; внешний
download сверх общей бесплатной квоты оплачивается отдельно. У COLD нет
минимального годового срока, у ICE удаление раньше 12 месяцев требует доплаты.

## State, план и применение

Ни `*.tfvars`, ни state/plan, ни credentials не должны попадать в Git/CI artifacts.
Параметры в `terraform.tfvars.example` — открытый пример; имя bucket обязательно
заменить. Ёмкость bucket и storage class заданы явными входами.

Для одного trusted deployment host используется local backend с native lock
в приватном каталоге вне рабочей копии. Все операторы работают на этом же
хосте и с одним state path. `flock` дополняет, но не заменяет Terraform lock.
Не копировать state на ноутбук для параллельного apply и не использовать
`-lock=false`. Резервную копию state хранить зашифрованно вне VM отдельно от
ротации игровых снимков. Для нескольких deployment hosts сначала настроить
backend с проверенной общей блокировкой; один S3 backend без механизма locking
недостаточен. Не хранить этот state в создаваемом backup bucket.

```sh
umask 077
install -d -m 0700 /var/lib/pz-terraform/backup-cloud
terraform init -backend-config=path=/var/lib/pz-terraform/backup-cloud/terraform.tfstate
terraform fmt -check
terraform validate
terraform test
terraform plan -input=false -var-file=/private/backup-cloud.tfvars -out=/var/lib/pz-terraform/backup-cloud/review.tfplan
terraform show /var/lib/pz-terraform/backup-cloud/review.tfplan
terraform apply -input=false /var/lib/pz-terraform/backup-cloud/review.tfplan
```

В `provisioner_principal_id` передать точный ID `yc iam whoami`. Эта уже
доверенная учётная запись получает policy Allow только на ARN bucket для
чтения конфигурации и записи multipart lifecycle. Wildcard `s3:*`,
DeleteBucket, изменение policy/ACL и доступ к object ARN правилом не добавляются.

Provider закреплён на `yandex-cloud/yandex 0.228.0`, lock совпадает с `cloud/`.
Использовать короткоживущий `YC_TOKEN`/доверенную авторизацию provider из
окружения. Terraform не создаёт static access keys: иначе secret попадёт в state.
План этой новой конфигурации должен показывать ровно bucket, три account, три bucket IAM bindings; не должно быть VM, удаления или замены storage. В CI `terraform test`
использует только mock provider и не требует настоящих credentials.

Если ресурсы уже существуют, сперва сопоставить IDs, свойства и владельца state.
Нельзя импортировать один ресурс сразу в несколько roots. Импорт требует private
tfvars, затем полный plan; несоответствия исправить до apply:

```sh
terraform import -var-file=/private/backup-cloud.tfvars yandex_storage_bucket.backup REVIEWED_BUCKET
terraform import -var-file=/private/backup-cloud.tfvars 'yandex_iam_service_account.actor["uploader"]' REVIEWED_UPLOADER_ID
terraform import -var-file=/private/backup-cloud.tfvars 'yandex_iam_service_account.actor["retainer"]' REVIEWED_RETAINER_ID
terraform import -var-file=/private/backup-cloud.tfvars 'yandex_iam_service_account.actor["restore"]' REVIEWED_RESTORE_ID
```

Не импортировать bucket, где когда-либо включали versioning/Object Lock, в эту
конфигурацию без отдельной адаптации протокола и плана очистки старых versions.
Новый bucket создаётся unversioned по умолчанию: явный
`versioning { enabled = false }` не задаётся, поскольку он вызывает
`PutBucketVersioning`, который может быть недоступен с user IAM token.
Текущий протокол работает с уникальными unversioned keys, требует conditional
PUT, полную SHA256 проверку GET и публикует marker последним.

## Учётные записи и шифрование

| Account | Разрешено только в `pz/<environment>/` |
| --- | --- |
| uploader | PUT, GET/readback, multipart start/parts/complete/abort/list |
| retainer | GET, LIST и DELETE точных объектов после успешного drill |
| restore | GET и LIST |

Для всех разрешён read-only `GetBucketVersioning` на именованный bucket, чтобы
протокол мог отвергать неподдерживаемое состояние. LIST требует явно переданный
prefix. Uploader дополнительно запрещено удаление; policy требует
`If-None-Match` для upload/completeUpload. Baseline IAM роли задаются только на bucket: `storage.uploader`,
`storage.editor` и `storage.viewer` соответственно; bucket policy ограничивает
их действия и prefix. Никому из runtime accounts не выдаются
`DeleteBucket`, изменение policy/ACL, доступ к другим prefixes или folder-роли.
HTTPS обязателен, anonymous read/list/config отключены. Lifecycle удаляет только
незавершённые multipart старше 7 суток, не завершённые snapshots.

Выдать каждому account отдельный static S3 key вне Terraform и доставить в
его root-owned credential-файл `0600`; не назначать accounts как VM identity.
Ключи не выводить в shell history/logs. Restore key и приватный age identity
хранятся вне VM; VM нужен только публичный age recipient для шифрования.
Для restore drill доставить identity временно в закрытый каталог и удалить его
после проверки, сохранив внешнюю копию. Ротация S3 key: новый key → private file →
проверка прав → отзыв старого key. Не смешивать retainer key с uploader процессом.

Архив и приватный manifest шифруются `age` до S3. `kms_key_id` опционально
добавляет SSE-KMS с уже существующим ключом; права на него задаются отдельно
на самом ключе, без `kms.admin` на folder. Перед включением проверить encrypt и
decrypt каждого нужного account и восстановление после потери VM. SSE-KMS
сам по себе не заменяет внешнюю копию age identity.

Перед рабочими backups выполнить fixture на одноразовых объектах: PUT/GET,
multipart/abort и conditional overwrite denial; retainer DELETE только fixture;
restore PUT/DELETE denial; все actors — запрет другого prefix, непредикатного LIST,
policy/ACL/delete-bucket. Mock-тесты подтверждают сформированную policy, но не
заменяют проверку фактических ответов endpoint. Не испытывать DeleteBucket или
изменение policy уничтожающим запросом на рабочем bucket: использовать отдельный
одноразовый тестовый bucket с той же policy либо безопасный IAM review.

Схемы и права сверены с [provider 0.228.0](https://github.com/yandex-cloud/terraform-provider-yandex/blob/v0.228.0/yandex/resource_yandex_storage_bucket.go),
[bucket policy](https://yandex.cloud/en/docs/storage/security/policy),
[списком S3 actions](https://yandex.cloud/en/docs/storage/s3/api-ref/policy/actions)
и [подключением дисков](https://yandex.cloud/en/docs/compute/operations/vm-control/vm-attach-disk).
