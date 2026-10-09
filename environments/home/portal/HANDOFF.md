# Передача Portal/PostgreSQL в центральный infra

Состояние на 2026-10-09. Источник: `/home/m4tveevm/PycharmProjects/portal/infra/k3s`.
Получатель: `/home/m4tveevm/PycharmProjects/infra`; копирование и коммит выполняет
агент общей инфраструктуры. Эта передача не включает применение конфигураций,
перезапуск служб или переключение Portal. Общий Caddy меняет только его текущий
владелец — агент общей инфраструктуры.

## Состав исходников

Копировать только файлы из `HANDOFF.sha256` и сам этот список. Все перечисленные
файлы можно оставить соседями в одном каталоге центрального репозитория.
Проверка после копирования: `sha256sum -c HANDOFF.sha256` в каталоге назначения.

| Файлы | Статус | Назначение и зависимости |
| --- | --- | --- |
| `postgres.yaml` | Live | Namespace, PV/PVC, Service, NetworkPolicy и StatefulSet PostgreSQL |
| `verify-postgres.sql` | Проверено на live БД | Проверка ролей, схем и изоляции; пробные записи внутри rollback |
| `backup-postgres.sh`, `encrypt-postgres-backup.sh` | Live на VM | Ежедневный dump и шифрование; оба устанавливаются в `/opt/updspace-data/` |
| `updspace-postgres-backup.service`, `updspace-postgres-backup.timer` | Live на VM | Установлены в `/etc/systemd/system/`; timer активен, последний результат success |
| `sync-postgres-backups.py` | Live на рабочем компьютере | Установлен исполняемым в `$HOME/.local/share/updspace-backups/portal-postgres/` |
| `updspace-postgres-offsite.service`, `updspace-postgres-offsite.timer` | Live на рабочем компьютере | Установлены в `$HOME/.config/systemd/user/`; timer активен, последний результат success |
| `test_sync_postgres_backups.py` | Тесты | Читает соседний `sync-postgres-backups.py`; Python 3.11+ без внешних пакетов |
| `render-portal.py`, `portal-images.json` | Stage, только dry run | Генератор читает JSON рядом с собой; восемь production images закреплены digest |
| `portal-network.yaml` | Stage, только dry run | Будущие правила сети Portal; не применены |
| `HANDOFF.md` | Документация | Границы передачи, runtime dependencies и проверки |

Не копировать `source-inventory.json`, `storage-browser-acceptance.json`, архивы,
runtime dumps, `.env`, kubeconfig, Terraform state или содержимое `/tmp`.
Генератор больше не зависит от снимка облачной инвентаризации. Полная историческая
запись остаётся в Portal `README.md`, но не входит в этот ограниченный пакет.

## Существующие ресурсы и внешние зависимости

VM: `updspace_m4tveevm@192.168.1.176`, node `updspace-home`.
Одна прикладная PostgreSQL БД `updspace`, endpoint
`postgres.updspace-data.svc.cluster.local:5432`, pod `updspace-data/postgres-0`.
PV `updspace-postgres`, PVC `updspace-data/postgres`, данные
`/srv/updspace/postgres`, policy `Retain`. 30 GiB в PV не являются дисковой квотой.
Лимит PostgreSQL: 2 CPU / 2 GiB; request: 250m / 512 MiB.

Namespace `updspace-data` общий с Garage и ID YDB. PostgreSQL NetworkPolicy
выбирает только postgres pod. Не удалять namespace и не добавлять общую deny
policy без проверки остальных сервисов. ID сохраняет свою локальную YDB по
отдельному выбору пользователя; все восемь сервисов Portal используют одну
PostgreSQL БД и разные схемы.

Существующие роли/схемы: `id` (резерв), `portal_bff`, `portal_access`,
`portal_core`, `portal_activity`, `portal_events`, `portal_voting`,
`portal_gamification`, `portal_featureflags`. Каждая роль владеет своей схемой,
имеет `search_path=<own_schema>,pg_catalog`; чужие схемы и `public` закрыты.
Создание ролей и выдача прав ранее выполнены отдельно: **этот пакет не является
полным bootstrap с нуля**. При принятии IaC нужно отдельно оформить идемпотентное
управление ролями и runtime secrets, сохранив существующие значения.

Защищённые runtime prerequisites, не входящие в Git:

- Secret `updspace-data/postgres-admin`, ключ `password`;
- root-only `/opt/updspace-data/credentials.json`, каталог 0700, файл 0600;
- публичный `/opt/updspace-data/backup-recipient.asc` и GPG home
  `/opt/updspace-data/gpg/` на VM;
- закрытый ключ только на рабочем компьютере, в
  `$HOME/.local/share/updspace-backups/keys/`;
- существующий SSH-доступ рабочего компьютера к VM с необходимым `sudo -n`.

Не переинициализировать PVC, не менять пароли и не пересоздавать Secret при
простом переносе исходников. Проверять существование и права каталогов при
будущем оформлении установки. Скриптам VM нужны bash, k3s/kubectl, tar, sha256sum,
GnuPG; синхронизации нужны Python 3.11+ и OpenSSH. На VM доступен только публичный
ключ шифрования; закрытый ключ на VM не переносился.

Бэкапы VM: `/srv/updspace/backups/postgres/<UTC timestamp>-<suffix>/`.
`COMMITTED` на VM означает полный dump; для передачи также нужен
`postgres.tar.gpg.sha256`. На компьютер передаётся только ciphertext. Локальный
`COMMITTED` создаётся после проверки SHA256. Архивы автоматически не удаляются.
Timer VM: ежедневно 03:15 UTC с задержкой до 300 секунд; timer компьютера:
ежечасно с задержкой до 300 секунд. Оба `Persistent=true`.

## Portal пока остаётся в облаке

Namespace `updspace-portal` создан, Deployment/Service/CronJob/NetworkPolicy
в нём отсутствуют. Генератор выпускает 8 Deployment с `replicas: 0`, 8 Service
и 14 CronJob с `suspend: true`. Ничего из этого пакета не включает Portal
автоматически. Registry и runtime Secret пока отсутствуют: `source-registry`
и `<service>-runtime` являются только ссылками в манифесте.

`portal-network.yaml` разрешает внутренний TCP 8000, DNS, PostgreSQL, Garage и
публичный HTTPS. BFF принимает edge pod с меткой `app.kubernetes.io/name=caddy`.
Перед запуском сверить реальные метки и маршруты Caddy. `/health` проверяет
процесс, а не целостность импорта или готовность PostgreSQL.

Ещё не перенесены бизнес-данные, media, frontend bundle и облачные runtime keys;
не переключены DNS, публичные маршруты или фоновые обработчики. Отдельное
разрешение на экспорт существующих Portal Lockbox secrets и временного registry
токена на VM остаётся ожидаемым после отказа автоматической проверки. Разрешение
пользователя на зашифрованные Portal backups уже получено и реализовано.

## Проверки передачи

- Хеши девяти live исходников совпали с установленными файлами VM/компьютера.
- Генератор после отделения списка образов выдаёт побайтово тот же manifest:
  SHA256 `e5b795b549bafeb3f275318d72a41408349fada0db633eb6199c1d14e2497616`.
- Изолированная копия только генератора и списка образов даёт тот же результат.
- Server-side dry run всех Portal workloads и обеих NetworkPolicy прошёл.
- `bash -n backup-postgres.sh encrypt-postgres-backup.sh` прошёл.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py' -v`
  из этого каталога: 5 тестов прошли; проверены SHA256, повреждения, отклонение
  недопустимых имён и сохранность готовых копий.
- Ранее проверены live SQL isolation и NetworkPolicy реальными pods. Пустой
  PostgreSQL dump зашифрован, передан на компьютер, расшифрован только там и
  восстановлен отдельно; совпали 9 схем и владельцев. Бизнес-данных Portal
  в этой пробной копии нет, после импорта требуется новая проверка восстановления.

Для повторной проверки схем Kubernetes без изменений: вывести результат
`python3 render-portal.py` в `kubectl apply --dry-run=server -f -`, отдельно
`kubectl apply --dry-run=server -f portal-network.yaml` в контексте этой VM.
Не снимать `--dry-run=server` при проверке передачи.
