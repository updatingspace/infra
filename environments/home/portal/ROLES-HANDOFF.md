# Дополнение IaC: роли PostgreSQL

Дополнение к `HANDOFF.md`, 2026-10-09. Копировать файлы из
`ROLES-HANDOFF.sha256` и сам этот список рядом с ранее принятыми исходниками в
центральном infra. Исходный пакет `HANDOFF.sha256` не изменялся.

## Декларация и режимы

`postgres-roles.json` задаёт существующую БД `updspace` и девять ролей, каждая
владеет одноимённой схемой. Роль `id` — резерв; рабочий ID использует YDB.
`manage-postgres-roles.py` читает JSON рядом с собой. Требует Python 3.11+,
k3s и доступ администратора PostgreSQL через локальный socket в
`updspace-data/postgres-0`. На VM от root:

```sh
sudo python3 manage-postgres-roles.py
sudo python3 manage-postgres-roles.py --check
```

Оба варианта выполняют только `BEGIN READ ONLY` и запросы к системным каталогам.
Пароли, `pg_authid`, credentials-файлы, Secret, PVC и бизнес-таблицы при проверке
не читаются. JSON содержит имена ролей, отклонения и блокирующие конфликты.
Exit 0 означает соответствие, exit 1 — отклонения. Ошибка сервера также даёт
ненулевой код; SQL и stderr скрыты, поскольку при bootstrap могут содержать пароль.

Явное применение (на рабочей БД в рамках передачи **не запускалось**):

```sh
sudo python3 manage-postgres-roles.py --apply
```

Скрипт разрешает LOGIN, запрещает SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS,
предоставляет только CONNECT к БД и USAGE/CREATE к своей схеме, закрывает чужие
управляемые схемы и `public`, снимает PUBLIC grants с БД/управляемых схем и
устанавливает `search_path=<role>,pg_catalog` для БД `updspace`.
Применение — одна транзакция с проверкой результата перед COMMIT. При корректном
состоянии `--apply` не отправляет изменяющий SQL и не читает credentials.

Неожиданный владелец схемы, владение БД прикладной ролью, членство в ролях или
доступ к посторонней схеме блокируют применение. Скрипт не забирает чужую
собственность и не удаляет членства автоматически. Конфликт после начального
audit обнаруживается перед COMMIT и откатывает всю транзакцию.

Пароли существующих ролей не читаются, не сравниваются и не меняются.
Роли/схемы/таблицы не удаляются, владельцы не меняются, таблицы не переносятся.
Удаление роли из декларации не удаляет её из PostgreSQL. Это управление
ролями/схемами, а не миграциями таблиц Django.

## Bootstrap только с внешними credentials

Отсутствующие роли требуют внешнего JSON `{role: password}`:

```sh
sudo python3 manage-postgres-roles.py --apply --credentials /opt/updspace-data/credentials.json
```

Файл должен быть обычным, принадлежать оператору и не иметь прав для группы и
остальных (0600 подходит); symlink отклоняется. Новым ролям нужны пароли от 16
символов без управляющих знаков. Ключ `postgres` и значения существующих ролей
игнорируются. Если новых ролей нет, файл не открывается. Пароли не генерируются,
не печатаются и не сохраняются скриптом; секретный JSON в пакет не входит.
БД, admin Secret и PVC должны уже существовать.

`--pod` позволяет проверить другой PostgreSQL pod в том же namespace, по
умолчанию `postgres-0`. Скрипт не создаёт pod и не меняет endpoint приложений.

## Проверки

- Live read-only audit: 9 ожидаемых ролей, `issues=[]`, `blockers=[]`.
- 8 unit tests: read-only default, no-op apply, drift, имена, сохранение паролей,
  внешние credentials, режим файла/symlink и отказ при конфликте.
- `check-postgres-roles-integration.py` выполнен на VM в отдельном временном pod
  с тем же закреплённым образом PostgreSQL 18.6. Только emptyDir, без Service/PVC,
  сервер слушает loopback, удаление pod в `finally`.
- Прошли bootstrap с синтетическими credentials, отказ без них, идемпотентность,
  исправление grants/search_path, отказ при неожиданном владельце/членстве,
  rollback при конфликте между audit и apply. Отпечаток тестовых password hashes
  и тестовая строка сохранились. Pod удалён; рабочие службы не перезапускались.

Повторный unit test из каталога файлов:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_manage_postgres_roles.py' -v
```

Явная интеграционная проверка на VM:

```sh
sudo python3 check-postgres-roles-integration.py
```

Последняя команда создаёт временный pod: это не read-only операция кластера.
Нужно до 512 MiB памяти и 256 MiB emptyDir; trust-auth доступен только через
loopback/exec этого pod. Рабочие credentials не используются.

Live apply не выполнялся: БД уже соответствует декларации. Перенос прикладных
данных Portal, Django migrations и production cutover этим дополнением не проверены.
