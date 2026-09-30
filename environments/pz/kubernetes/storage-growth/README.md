# Увеличение файловой системы Zomboid: 26 → 50 GiB

Подготовленный этап, **ещё не применённый**. Владелец самостоятельно увеличивает
облачный SSD с 60 до 100 GiB. Этот root не обращается к облачному API и не меняет
VM, разделы, другие окружения или данные мира. Он выполняется **на самой VM**
в отдельном каталоге/state, например `/opt/pz-infrastructure/storage-growth`.
Исходный `storage/` остаётся историей завершённой миграции на 26 GiB: его
конфигурацию, state и `/var/lib/pz-volumes/migration.json` не изменять и миграцию
повторно не запускать.

На 30 сентября проверена схема: `/dev/vda` 60 GiB, ext4 root на `/dev/vda1`,
Zomboid — `/var/lib/pz-volumes/zomboid.ext4`, 26 GiB, UUID
`0bd0ee81-1d96-447e-868a-405226663cf6`, mount `/srv/pz-storage/zomboid`.
Номер loop-устройства определяется по mount/backing file; он может измениться
при загрузке. Root UUID также закреплён в helper. Замена диска/ФС требует
новой проверки, а не отключения этих ограничений.

## После подтверждения увеличения облачного диска

Сначала проверить фактический размер и разметку. Команды ниже не относятся к
подготовке и выполняются только после завершения изменения владельцем:

```sh
sudo blockdev --getsize64 /dev/vda     # не менее 107374182400 (100 GiB)
sudo lsblk -b -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,START
sudo findmnt --mountpoint / -o SOURCE,FSTYPE,UUID,TARGET
sudo growpart --dry-run /dev/vda 1
```

Если `/dev/vda1` ещё не расширен, изучить dry-run: меняется только конец
раздела 1, начало и EFI-разделы сохраняются. Затем `sudo growpart /dev/vda 1`.
Если раздел уже вырос, этот шаг пропустить. При несовпадении kernel partition
size с таблицей разделов остановиться и отдельно решить вопрос перечитывания
таблицы/перезагрузки; не отсоединять loop и не форсировать resize.

После подтверждения увеличенного `/dev/vda1` выполнить
`sudo resize2fs /dev/vda1`, если корневая ext4 ещё занимает прежний размер.
`grow.py --check` требует не менее 99 GiB **и раздела, и ext4**: облачного resize
самого по себе недостаточно. Обновить `boot_disk_gib=100` в отдельном cloud
tfvars до следующего cloud plan, чтобы Terraform не предлагал вернуть 60 GiB.

## Terraform-этап

Сохранить state/plan/log с правами 0600 и каталог с правами 0700. Подготовить
актуальную проверенную резервную копию; прежний полный архив не удалять.

```sh
# На VM, из отдельного каталога с main.tf и grow.py:
umask 077
sudo python3 grow.py --check
sudo terraform init
sudo terraform plan -out=reviewed.tfplan
```

План должен создавать только `terraform_data.zomboid_50g`. Применять сохранённый
план под отдельным detached systemd service с `RemainAfterExit=yes`, приватным
логом и уникальным именем по SHA256 плана; Terraform и helper должны продолжать
работать при обрыве SSH. Не использовать интерактивный SSH как родителя apply.
После apply проверить exit 0 сервиса, `sudo python3 grow.py --check`:
`complete=true`, и повторный Terraform plan без изменений.

Helper берёт существующий `migration.lock`, проверяет UUID, mount, единственный
loop/backing mapping, offset/sizelimit=0, отсутствие shrink и завершённую старую
миграцию. Затем `posix_fallocate` резервирует весь файл 50 GiB на root ext4,
сохраняя содержимое; `losetup --set-capacity` обновляет loop, `resize2fs ... 50G`
увеличивает смонтированную ext4. Обязателен запас 8 GiB на корне плюс ещё
не выделенные физически блоки edge/observability. Данные игры не копируются.
Этот этап не меняет Kubernetes PV/PVC или ResourceQuota: их номинальную ёмкость
согласовать отдельным просмотренным планом; они не ограничивают этот общий ext4.

## Прерывание и восстановление

Собственный журнал — `/var/lib/pz-volumes/growth-zomboid-50.json`; каждый запуск
проверяет действительные размеры. Файл уже 50 GiB, loop ещё 26: обновить loop
тем же helper. Loop 50, ext4 ещё 26: тот же helper завершает online resize.
Все три слоя 50 и файл физически выделен: helper ничего не меняет. При ошибке
resize не уменьшать файл/loop, не запускать mkfs или e2fsck на mounted ext4.
Сначала проверить service/journal и идентичность слоёв; явно возобновлять тот же
helper только после окончания предыдущего процесса.

При неуспешном provisioner Terraform может пометить запись tainted;
`prevent_destroy` намеренно блокирует замену. Сначала завершить и подтвердить
рост, затем отдельно согласовать восстановление Terraform state. Не обходить
защиту автоматическим destroy/recreate. Trim/discard внутри loop может снова
сделать образ разреженным; проверять `image_allocated_bytes` и host free space.

Источники: [расширение диска и раздела Yandex Cloud](https://yandex.cloud/en/docs/compute/operations/disk-control/update),
[online ext4 resize](https://man7.org/linux/man-pages/man8/resize2fs.8.html),
[перечитывание ёмкости loop](https://man7.org/linux/man-pages/man8/losetup.8.html),
[предварительное выделение места](https://man7.org/linux/man-pages/man3/posix_fallocate.3.html).
