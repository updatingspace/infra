# Облачная VM, загрузочный диск и security group

Этот отдельный Terraform root управляет существующими VM `fv4g468rskpfcem690fs`
и SSD `fv4m82qqrtj4rq7ppj00`: **4 vCPU, 16 GiB RAM, 60 GiB disk**.
Импорт выполнен 30 сентября 2026 года. Затем применена security group
`enpp121nmc0ggbt5hd5g` (`pz-k3s-server`): создана группа и изменена только её
привязка к NIC существующей VM. **CPU, RAM и размер диска не изменены.**
Сводка последней проверки без metadata — `verification.json`; фактические
правила и привязка — `security-group-live-verification.json`.
Повторный план после применения завершился кодом `0`: все три ресурса — no-op.

Существующие subnet и адреса сохранены; новых VM и дисков не создано.
`prevent_destroy` установлен на VM, диск и security group. Существующее
`boot_disk.auto_delete = true` сохранено, поэтому защита Terraform не заменяет
резервные копии и не защищает от удаления VM через облачную консоль.

SSH/OS Login metadata остаётся под существующим управлением: только `metadata`
исключена через `ignore_changes`. Параметры доступа к metadata endpoint,
CPU, RAM, размер диска и настройки сети заданы явно и не скрываются от плана.
Сам импорт включает metadata в state: state и сохранённые планы имеют права
`0600`, исключены из git родительским `.gitignore` и не должны публиковаться.
Этот state независим от `environments/pz/terraform`, где находятся IAM/Monium.

На NIC установлена только новая группа; default SG к ней не добавляется.
Разрешён входящий IPv4: **TCP 22/80/443, UDP 443/16261–16262 и ICMP**.
Исходящий IPv4 разрешён. Публичные Kubernetes API `6443`, kubelet `10250` и
NodePort не разрешены. Private bind сам по себе не закрывает публичный доступ:
облачный one-to-one NAT направляет его на частный адрес VM.

После применения подтверждены новое SSH-соединение и Ready-узел; внешние
TLS/HTTP-пробы API и kubelet завершились сбросом соединения без HTTP-ответа.
Один успешный TCP `connect()` в используемой сети не считается доказательством
доступности сервиса. Текущий кластер одновузловой; правила для других узлов
нужно согласовать отдельно. Обновление только списка SG поддерживается без
остановки VM, а `allow_stopping_for_update=false` остаётся заданным.
[Изменение SG интерфейса](https://yandex.cloud/en/docs/compute/operations/vm-control/vm-change-security-groups-set),
[правила и stateful-соединения](https://yandex.cloud/en/docs/vpc/concepts/security-groups).

Для проверки после настройки `yc`:

```sh
cd environments/pz/kubernetes/cloud
umask 077
export YC_TOKEN="$(yc iam create-token)"
terraform init
terraform validate
terraform plan -detailed-exitcode
unset YC_TOKEN
```

Код завершения плана `0` означает отсутствие изменений, `2` — наличие изменений.
В новом checkout сначала восстановите защищённый state. Если state ещё никогда
не переносился в этот checkout, существующие ресурсы можно импортировать без
изменений в облаке:

```sh
terraform import yandex_compute_disk.boot fv4m82qqrtj4rq7ppj00
terraform import yandex_compute_instance.server fv4g468rskpfcem690fs
terraform import yandex_vpc_security_group.server enpp121nmc0ggbt5hd5g
terraform plan -detailed-exitcode
```

`imports.tf` также фиксирует ID VM и диска для декларативного импорта. Нельзя принимать
план создания или замены этих ресурсов вместо импорта. Для будущего увеличения
ресурсов измените только значения из `terraform.tfvars.example`, проверьте план
и подготовьте обслуживание. Остановка провайдером по умолчанию запрещена;
расширение облачного диска отдельно требует проверки размера раздела/файловой
системы внутри VM. После увеличения VM пересчитайте application budget в
`../platform`; облачная ёмкость не меняет Kubernetes-квоты автоматически.

Документация провайдера: [VM и импорт](https://yandex.cloud/en/docs/terraform/resources/compute_instance),
[диск](https://yandex.cloud/en/docs/terraform/resources/compute_disk).
