# Параметры существующего домашнего PZ

Публичные значения приняты из live root-only tfvars по allowlist. Секретов здесь
нет. Запускать Terraform из соответствующего каталога `environments/pz/kubernetes`:

```sh
# cwd: environments/pz/kubernetes/workloads
terraform plan -var-file=../../../home/pz-values/workloads.json
# cwd: environments/pz/kubernetes/observability
terraform plan -var-file=../../../home/pz-values/observability.json
# cwd: environments/pz/kubernetes/platform
terraform plan -var-file=../../../home/pz-values/platform.hcl
```

Это команды review, не разрешение на слепой apply. Нужны существующий state и
доступ к kubeconfig VM; workstation path в примерах не предполагается. Проверять
план на удаление/replacement и актуальный shared edge. State не входит в Git;
не импортировать/создавать поверх него второй независимый state.

`allocatable` в platform — выделенный бюджет PZ/edge/observability, не физическая
ёмкость всего узла. Остальные приложения имеют отдельные бюджеты. Исторический
storage Terraform на 26 GiB не описывает уже существующие game/spool 64 GiB;
реальные mounts/UUID находятся в `../host/storage.json`, форматировать их нельзя.
На локальной VM backup/collector не используют старый Monium Secret.

Критичные overrides Caddy: canonical config, `/srv/edge-caddy`, monitoring auth
Secret и TLS Secret для закрытой пробы ID. Их потеря может сломать общий edge.
Клонированные локальные images должны уже присутствовать в k3s; эти values не
разрешают новое скачивание latest или одновременный upgrade компонентов.
