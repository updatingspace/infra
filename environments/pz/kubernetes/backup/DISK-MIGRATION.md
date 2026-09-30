# Перенос данных на отдельный SSD

`disk-migrate.py` — отдельная операция после успешного S3 backup и полного
изолированного restore drill. Он не вызывается timer и не изменяет облачные
ресурсы. Старый `/var/lib/pz-volumes/zomboid.ext4` не удаляется, не форматируется
и не уменьшается. По завершении он смонтирован read-only в
`/srv/pz-storage/zomboid-old`. Объём SSD сейчас выбран 40 GiB по доступной квоте;
spool 64 GiB находится на другом устройстве.

Подключать cloud disk с `device_name`, равным его ID. Внутри VM проверяется
`/dev/disk/by-id/virtio-<ID>`, точное совпадение SERIAL и размера. Диск должен
быть новой пустой облачной заготовкой без `image_id`/`snapshot_id`; проверки
подписей не доказывают отсутствие произвольных неизвестных raw-данных.
Операция `initialize` требует явно повторить ID и отвергает существующую ФС,
partition table, дочерние partitions, mountpoints или holders. Никогда не
передавать ей boot disk либо существующий loop/device.

```sh
python3 /opt/pz-backup/disk-migrate.py --expected-host REVIEWED_HOST initialize \
  --disk-id REVIEWED_NEW_DATA_ID --size-gib 40 --label pz-data \
  --initialize-empty-disk REVIEWED_NEW_DATA_ID
python3 /opt/pz-backup/disk-migrate.py --expected-host REVIEWED_HOST initialize \
  --disk-id REVIEWED_NEW_SPOOL_ID --size-gib 64 --label pz-backup-spool \
  --initialize-empty-disk REVIEWED_NEW_SPOOL_ID
```

Spool монтируется отдельной командой по возвращённому UUID:

```sh
python3 /opt/pz-backup/disk-migrate.py --expected-host REVIEWED_HOST mount-spool \
  --disk-id REVIEWED_NEW_SPOOL_ID --size-gib 64 --target-uuid REVIEWED_SPOOL_UUID
```

Она проверяет serial/размер, ext4 label `pz-backup-spool`, UUID, пустой mountpoint
и отсутствие постороннего mount. Существующая несовпадающая запись fstab
отвергается. В fstab добавляется UUID в `/srv/pz-backup-spool` с
`nodev,nosuid,noatime`, без `nofail`; форматирование эта команда не выполняет.
Указать UUID в coordinator config, права spool выставляет installer.
Первый off-host backup создаётся ещё с оригинального loop. Затем выполнен
полный restore с приватным запуском нужного build, RCON health и загруженным
миром; attestation должен пройти проверки `remote.py`, относиться к тому же
bucket/prefix/endpoint/format и быть не старше 24 часов.

Для переноса смонтировать новый пустой data SSD по UUID в
`/srv/pz-data-migration` с `nodev,nosuid,noatime`. Каталог
`/srv/pz-storage/zomboid-old` должен существовать и быть пустым немонтированным
каталогом. Target может содержать только пустой `lost+found`. Установить
`disk-migrate.py`, `coordinator.py`, `remote.py` как root-owned файлы в
`/opt/pz-backup`; production helper сравнивает свой код с установленным helper
перед добавлением boot guard.

Запускать отдельным systemd service в выбранное окно: потеря SSH не должна
прерывать перенос. Команда начинает простой и сама сохраняет исходные replicas
и состояние updater в приватном persistent journal:

```sh
systemd-run --unit=pz-disk-migration --service-type=exec --no-block \
  --property=UMask=0077 --property=TimeoutStartSec=0 \
  /usr/bin/python3 /opt/pz-backup/disk-migrate.py --expected-host REVIEWED_HOST migrate \
  --config /etc/pz-backup/config.json \
  --remote-config /etc/pz-backup/cleanup-remote.json \
  --attestation /var/lib/pz-backup/restore-attestation.json \
  --disk-id REVIEWED_NEW_DATA_ID --size-gib 40 --target-uuid REVIEWED_DATA_UUID
```

Перенос держит общий `/var/lib/pz-volumes/migration.lock`, блокирует updater,
дожидается его Jobs/Pods и терминального journal, останавливает collector,
панель и игру через replicas=0. Collector имеет вложенный RW mount через
`/hostfs`, несмотря на read-only верхнего bind mount. Helper ждёт освобождения
kubelet/container mounts и открытых файлов; запуск требуется в host mount
namespace без `PrivateMounts`/`ProtectSystem`/`PrivateTmp`.
До копирования требует наблюдаемое корректное завершение
игрового процесса и отсутствие писателей; при неопределённом исходе оставляет
их остановленными для разбора. Затем remount источника read-only, полный
`rsync -aHAX --numeric-ids --one-file-system --sparse` без исключений и сверка
SHA256, списка файлов, UID/GID, modes, hardlinks, symlinks и xattrs/ACL.
Игра уже остановленная до переноса не запускается после него. Копирование и
хэш-проверка ограничены 1800 секундами. При отказе до изменения fstab/boot guard
проверенный исходный mount возвращается в RW и исходные приложения запускаются;
неопределённый исход после переключения требует ручного разбора.

После проверки меняется только строка zomboid внутри существующего managed
fstab block, сохраняется `fstab.before`, добавляется k3s `RequiresMountsFor`
и `ExecStartPre` проверки точного UUID. Удаление/отсутствие диска блокирует
запуск k3s вместо записи на `/`. Выполняется обычный umount (никогда force/lazy),
mount новой ФС на прежний `/srv/pz-storage/zomboid`, старого образа read-only на
`zomboid-old`, обновление `data_uuid` coordinator и восстановление исходных
replicas/updater. PV paths и legacy symlinks остаются прежними.

При занятой старой ФС штатный umount откажет: проверить read-only consumers
логов/посторонние bind mounts, устранить причину в том же окне и продолжить
вручную по journal. Helper не угадывает восстановление после частичного cutover
и отвергает повторный `migrate` при существующем journal. Проверить UUID всех
mounts, phase и fstab перед любым продолжением. Не удалять journal лишь для
обхода этой проверки и не возвращать приложения на непроверенный mount.

Незавершённый disk journal блокирует backup и deploy, включая проверку внутри
общего lock непосредственно перед sync/plan/apply. Read-only status остаётся
доступным. Legacy storage/growth блокируются при любом disk journal и больше
не могут перезаписать новую конфигурацию loop-настройками.

После окончания проверить игру/панель, RCON/world, вместимость и права, новый
backup, restore configuration и Terraform plan. Старый `storage.py` описывает
первичный переход на loop: его повторное provision после переноса прекращается
до изменения fstab. Для нового диска используется отдельный backup-cloud state.
Mount старого loop обратно после новых сохранений потеряет изменения: сначала
остановить писателей, сделать свежий off-host backup и перенести актуальные
данные обратно с хэш-проверкой. Helper автоматического rollback и удаления
старого loop не выполняет.
