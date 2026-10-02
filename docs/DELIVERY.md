# Переход к доставке из GitHub Actions

Статус: Kubernetes, проверяемые S3 backups и обновление игры описаны в IaC;
последние изменения — в ветке `feat/pz-game-auto-update`. Production workflow
пока не настроен; GitHub Actions выполняет проверки.
Сейчас оператор запускает `environments/pz/kubernetes/deploy.py` по SSH. Каталог
на VM `/opt/pz-infrastructure` содержит небольшой рабочий bundle Terraform,
проверенный provider mirror, state и приватные vars; он остаётся необходимым.
История Git, тесты и исходники сборки на VM для обычного apply не нужны.

## Что нужно сделать до автоматического apply

1. Проверить diff ветки и остановить старую Swarm-доставку при слиянии: её
   workflow сохранён в `legacy/swarm/ci-cd.yml.disabled`. Не запускать старый
   `docker stack deploy` на PZ VM. Состояние Swarm-сервисов на других VM неизвестно.
2. Сохранить проверенные защищённые копии всех state и настоящих tfvars.
   Кроме локальных cloud, bootstrap, storage, Monium/IAM, появились отдельные
   backup-cloud и game-updater state. Три Kubernetes state — на VM:
   platform, workloads, observability. Не объединять их и не выполнять apply
   с пустым state. Git не является хранилищем state или secrets.
3. Выбрать state backend и блокировку. До отдельной проверенной миграции оставить
   state на своих местах; смена каталога исходников не требует `state mv`. Для
   удалённого backend нужны версионирование, шифрование, узкий доступ и проверенная
   блокировка. Не менять backend одновременно с изменением ресурсов.
4. Выбрать доступ runner к VM: отдельная учётная запись, проверка SSH host key,
   явно ограниченные команды. Текущий helper использует личный SSH key/path и
   требует параметризации перед CI. Не копировать личный ключ в репозиторий.
5. Начать с CI: fmt/validate, offline tests и проверка, что state/секреты не
   попали в diff. PR из forks не должен получать production credentials.
6. Production: один последовательный job на среду, environment approval,
   сохранённый проверенный plan с ограниченным доступом и коротким сроком
   хранения, apply именно его hash. Plan может содержать секреты. Не допускать
   одновременные operator и CI apply.
7. Сохранить существующую защиту workloads от гонки с panel updater: пауза Cron,
   ожидание завершения jobs, проверка terminal journal, plan/apply, возврат
   расписания. Ошибка не должна оставаться незамеченной, включая оставленную паузу.
8. После apply проверить Ready, mounts, фактические лимиты, RCON players, HTTPS
   панели и Monium. Изменения, требующие остановки игры, выполнять через save/quit
   с подтверждением завершения. Обычная доставка панели не перезапускает игру.

## Образы и восстановление

Game/Caddy/collector используют импортированные локальные образы. Git содержит
код игры, но её Dockerfile с upstream tag/apt не воспроизводит прежний digest
побитно. До удаления последней копии образа нужен защищённый registry или
проверенный OCI archive. Pipeline сборки/push/pinning — отдельный этап; менять
image policy и обновлять образы вместе с переносом исходников не требуется.

`import-images.py` и `sync-secrets.py` — однократные помощники миграции, не
доставка. Первый перезаписывает рабочие tfvars, второй читает старые snapshots;
их нельзя повторно запускать как часть CI.

## Резервные копии

Реализованы [проверяемые S3 backups](../environments/pz/kubernetes/backup/README.md)
и отдельные файловые системы данных/spool. Перед обновлением игры создаётся
свежий проверенный снимок через тот же coordinator. Доставка
[game updater](../environments/pz/kubernetes/game-updater/README.md) имеет свой
Terraform root. Все обслуживающие процессы используют общий maintenance lock;
незавершённый journal блокирует новый apply до восстановления оператором.
