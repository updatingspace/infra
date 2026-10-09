# Сохранённая база TeamSpeak

MariaDB 11.6.2 перенесена из единственного оставшегося Docker application container
в k3s namespace `teamspeak`, StatefulSet `mariadb`. Digest прежнего образа закреплён;
обновления версии нет. Requests 100m/128Mi, limits 500m/512Mi, namespace quota,
PV Retain `/srv/teamspeak/mariadb`, UID/GID 999. 2Gi capacity — учёт PVC, не квота ФС.

Голосовой TeamSpeak отсутствовал и не запускался. На момент переноса активных
клиентов БД не было. Service `mariadb.teamspeak.svc.cluster.local:3306` внутренний;
default-deny запрещает ingress/egress. При возвращении voice server нужно явно
добавить policy для его pods и задать новый endpoint, не открывая БД в интернет.

## Секреты и применение

Secret `teamspeak/mariadb-existing`, key `root-password`, содержит **прежний** пароль.
Он не генерируется заново и не входит в Git. `MYSQL_DATABASE=teamspeak` — несекретная
настройка. Восстановить прежние данные и Secret до применения `mariadb.yaml`.

```sh
sudo k3s kubectl apply --dry-run=server -f mariadb.yaml
sudo k3s kubectl apply -f mariadb.yaml
sudo k3s kubectl -n teamspeak wait --for=condition=Ready pod/mariadb-0 --timeout=120s
```

`OnDelete` исключает автоматическую перезагрузку DB после изменения template.
Смену образа/конфига проводить отдельно после backup, не удалять PVC/namespace.

## Выполненный перенос и откат

Закрытый каталог VM `/opt/updspace-infra/private/teamspeak-migration` содержит:
исходные параметры/restart policy, клиентский credentials file, checksum всех
28 таблиц, cold-backup.tar, его SHA256, 263 SHA файлов и acceptance.json.
База была чисто остановлена; архив восстановлен в **другой** каталог, все файлы
совпали. После старта прежний root password открыл БД, версия и checksums всех
28 таблиц совпали. `acceptance-2026-10-09.json` содержит только несекретный итог.

Старый container `teamspeak-docker-db-1` остановлен, restart policy `no`;
исходный Docker volume сохранён без удаления. Работающих Docker applications
на момент acceptance больше не было. Для отката: остановить k3s StatefulSet
(scale 0), дождаться удаления pod, сохранить появившиеся новые данные отдельно,
вернуть прежний restart policy из migration.json и запустить старый container.
После новых записей нужен обратный перенос данных; исходник — снимок момента
миграции, не синхронная реплика. Нельзя запускать оба как независимо пишущие БД.

`migration/prepare.py`, `cutover.py`, `verify.py` — точные операторские шаги этого
переноса. Они проверяют исходное состояние и отказываются повторять подготовку
или cutover после частичного выполнения. Не запускать их заново на принятой БД.
При сбое после остановки source проверить journal/каталоги и выбрать продолжение
или откат вручную. Live proof получен при переносе; отдельных unit tests этих
одноразовых шагов нет. Backup находится на той же VM; offsite restore не проверен.
