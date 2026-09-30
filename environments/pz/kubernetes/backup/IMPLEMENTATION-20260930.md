# Реализация backup: архитектура и проверки

Документ описывает код и порядок его применения. Фактические параметры
окружения, состояние rollout, результаты production-проверок и измерения
времени ведутся в отдельном закрытом отчёте оператора.
Требования к внедрению сохранены в [BACKUP-ROADMAP.md](../BACKUP-ROADMAP.md).

## Компоненты

| Компонент | Назначение и ограничения |
| --- | --- |
| [backup-cloud](../backup-cloud/README.md) | Отдельный Terraform root для дисков, bucket и IAM; `prevent_destroy`, закрытый state и файловая блокировка; размеры и storage class задаются явно |
| [Coordinator](coordinator.py) | Общий maintenance lock, persistent journal, проверка писателей и согласованной staging-копии перед возвратом приложений |
| [Uploader и retainer](remote.py) | Уникальные snapshot IDs, conditional writes, полный S3 readback по SHA256, marker COMMITTED последним; удаление только после проверки пяти сохраняемых комплектов |
| [Upload queue](upload-queue.py) | Повторная отправка готового spool; пустая очередь пропускается до цепочки cleanup/retention |
| [Restore](restore.py) и [runtime drill](runtime-drill.py) | Безопасное извлечение, полная проверка содержимого и metadata, изолированный запуск точного образа и мира |
| [Перенос диска](DISK-MIGRATION.md) | Проверка disk identity/UUID, остановка писателей, SHA и metadata, переключение mount и boot guard; исходная копия сохраняется |
| [Метрики](metrics.py) и [Monium templates](../observability/README.md) | Возраст commit, длительность фаз, свободные блоки/inode, ошибки и проверка восстановления |

Uploader, retainer и restore используют отдельные accounts и credentials.
Права ограничиваются bucket и backup prefix; постоянные ключи выдаются вне
Terraform и Git. Приватная age identity и restore credentials хранятся вне
игровой VM; для шифрования нужен public recipient. Архив и manifest шифруются
до отправки в S3.

Terraform states, recovery instructions и исходный архив миграции сохраняются
отдельно от ротации игровых копий. Операторский control bundle шифруется;
его проверка требует потоковой расшифровки и сравнения manifest/SHA без
открытых временных файлов. Ключи credentials и расшифрования хранятся отдельно.

## Порядок применения

1. Выполнить inventory и расчёт бюджета staging + архив + metadata + запас.
   Выбрать расписание, окно обслуживания, диски и storage class; проверить
   Terraform plan без пересоздания VM и удаления существующих данных.
2. Подготовить отдельные credentials и внешнюю копию age identity. Выполнить
   IAM-canary на одноразовых объектах; административные отрицательные права
   проверять безопасной fixture, без опасных запросов к рабочему bucket.
3. Вручную получить COMMITTED с полным удалённым readback. Независимо скачать
   комплект и выполнить полный isolated restore drill: архив, точный образ,
   изоляция, RCON, чтение существующего мира, моды и чистое завершение.
   Только реальный успешный drill позволяет записать связанную аттестацию.
4. Переносить данные отдельной операцией в согласованное окно. После cutover
   проверить UUID, fstab, k3s boot guard, права, health и соответствие IaC.
   Старую копию сохранить; прежние storage/growth provisioners повторно не применять.
5. Проверить backup после переноса, cleanup и свободный резерв. Включать
   расписание и retention только после этих gates; проверить естественное
   накопление пяти успешных комплектов.
6. Сохранить актуальный зашифрованный control bundle и приватный отчёт с
   доказательствами. Периодически повторять restore drill и проверять алерты.

Команды, форматы конфигурации и порядок восстановления приведены в
[backup README](README.md), [backup-cloud README](../backup-cloud/README.md)
и [инструкции переноса](DISK-MIGRATION.md).

## Особенности, которые учитывает код

- Plain ext4 не даёт согласованный live snapshot. Staging проверяется при
  остановленных писателях; упаковка и upload выполняются после их возврата.
- Read-only bind не гарантирует read-only вложенные mounts. Проверка полного
  освобождения staging mount выполняется после остановки collector, до RO/hash.
- Миграция требует read-only superblock источника, его исходные SHA и повторную
  проверку metadata, затем полные SHA/metadata назначения. Resume использует
  частичный target только как кеш и заново проверяет весь результат.
- Отсутствующее protobuf `exit_status` может означать нулевое значение;
  подтверждение выхода связывает exact container, событие и отсутствие процессов.
- Conditional multipart учитывает особенности endpoint. HEAD, ETag и metadata
  checksum не заменяют полный GET с проверкой длины и SHA256.
- Offline runtime использует проверенные nested mod bind mounts и canonical
  cache directory; production volumes и сетевые интеграции не подключаются.
- Opt-in filesystem-syncfs сохраняет проверки потока, дерева и metadata.
  Доверенный parent FD открыт до записи, ошибка барьера запрещает success report;
  default остаётся fsync каждого файла. Подробности — в [README](README.md#восстановление).

## Проверки кода

Локальный backup suite: **217 passed, 0 skipped**, включая настоящий rsync.
Проверки охватывают целостность архива, безопасное восстановление, блокировки,
сбои публикации и retention, cleanup и продолжение переноса. Команды запуска
приведены в [README](README.md#наблюдаемость-и-проверки).
CI выполняет проверки без production credentials и исключает private artifacts.
Результаты применения к окружению в этот документ не включаются.
