# ID и Portal: сокращение облака после переноса

2026-10-09 владелец разрешил сократить заменённые облачные установки после
проверки переноса. В этой же задаче он отменил хранение backup ID/Portal на
рабочем компьютере. Актуальная production-сторона — VM 192.168.1.176.

## Что удалено

- 13 Serverless Containers: 5 ID, 8 Portal.
- 6 остановленных triggers: 5 ID, 1 Portal.
- 2 API Gateway: ID и Portal.
- 1 Portal outbox function (её функция выполняется локальным CronJob).

Всего 22 ресурса; каждый сверялся с allowlist по id/name/folder перед удалением.
Изменение набора container revisions останавливает операцию. Перед запросом
и после получения operation id сохранялся журнал; неоднозначный результат
не повторяется автоматически. Завершение каждой операции и отсутствие
в свежем списке проверены. Точные ID/operation IDs — в
[cloud-retirement-2026-10-09.json](cloud-retirement-2026-10-09.json).

До удаления подтверждены proxied CNAME ID/Portal на updspacedd.tplinkdns.com,
готовность локальных сервисов и HTTPS с TLS verification. После удаления
публичные ID login/readyz и Portal homepage/CSRF API возвращают 200.
Полный вход реального пользователя этой очисткой не проверялся.

## Предотвращение повторного cloud deployment

В default branches GitHub три старых workflows переведены на manual trigger,
каждый их job имеет `if: ${{ false }}`. Обычные CI workflows не менялись.

- ID main: `2b534afdf5c2752278c1c159d970a0ff3f2185f0`.
- Portal master deploy: `1ddb007427bba02d755bd01ed8937145cc0bb539`.
- Portal master YDB migrations: `c4be18968d42e835e3473a3ce5dc5514f9269069`.

Свежий GitHub readback подтвердил guards и отсутствие активных запусков
этих workflows. GitHub workflow-level state остаётся active; jobs технически
запрещены в файлах. Ручной оператор может изменить эту политику; старый
Terraform не отражает удаления и при apply способен создать облако заново.
Не запускать его без нового принятого плана. История исходных файлов сохранена
в GitHub и в приватном архиве конфигурации на VM.

## Что сохранено в облаке

Обе YDB и все объекты S3 сохранены как замороженное состояние до переключения.
Общий registry, его образы, Lockbox, IAM accounts/permissions, VPC, log groups
и другие проекты не удалялись. Текущие ID/Portal runtime не используют эти
облачные ресурсы. Это сокращение runtime, а не обещание нулевого счёта за облако.

После новых записей на VM простой DNS rollback небезопасен. Кроме переноса
новых данных обратно, старый runtime теперь потребуется создать заново; resource
IDs изменятся. Приватный архив исходной конфигурации и плана находится на VM:
`/opt/updspace-cloud-retirement/20261009/config.tar.gpg`.

## Где остаются backup

Удалено 60 файлов, 1 028 148 331 байт, из пяти каталогов workstation
`~/.local/share/updspace-backups/{id,id-cutover,id-final-source,id-images,portal-postgres}`.
Два user timers disabled/inactive: updspace-id-offsite и updspace-postgres-offsite.
Другие проекты и общий keyring не удалялись. Ключ в `.../keys` сохранён намеренно:
это единственный известный private key для существующих зашифрованных копий.

Daily backup на VM остаётся включён: ID 02:30 UTC, Portal 03:15 UTC + jitter.
Новый Portal recovery bundle 1 382 747 218 байт также только на VM:
`/srv/updspace/backups/portal-recovery/20261009T162957Z/recovery.tar.gpg`.
Он включает два OCI archive, frontend, восемь собственных runtime Secrets,
восемь Portal DB roles, Garage key и frozen export. Временный IAM token и
credentials остальных компонентов исключены. Ciphertext SHA проверен на VM;
расшифровка этого нового bundle и перенос на ПК не выполнялись.

Исторические offsite restore proofs ID/Portal остаются доказательствами
проверенного переноса, но соответствующие архивы на ПК уже удалены.
Актуальной внешней копии новых данных нет. Backup на том же сервере/диске
не защищает от полной потери VM. Хранение вне VM требует другого выбранного
владельцем места, например NAS или отдельного backup-хранилища.
