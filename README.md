# Infrastructure

Актуальная инфраструктура PZ находится в [environments/pz](environments/pz/README.md):
одна VM Yandex Cloud, K3s, Terraform, отдельные бюджеты игры, панели, Caddy и Monium.
Исходники собраны из проверенной конфигурации работающей VM 30 сентября 2026.

- [Операторский runbook](environments/pz/kubernetes/README.md) — слои Terraform, ресурсы, безопасное применение и восстановление.
- [Подготовка GitHub Actions](docs/DELIVERY.md) — следующие шаги доставки и размещения state.
- [План S3 и дисков](environments/pz/kubernetes/BACKUP-ROADMAP.md) — план хранения последних пяти проверенных копий вне VM (ещё не внедрён).
- [Прежний Docker Swarm](legacy/swarm/README.md) — сохранённая конфигурация исходного репозитория; её доставка отключена в этой локальной версии.

Изменения собраны в ветке `chore/pz-k3s-iac` от `bc031eb`. Новый production
workflow пока не настроен. Старый Swarm workflow в `master` остаётся прежним
до слияния этой ветки; публикация ветки сама по себе не меняет production.

## State и секреты

Git содержит исходники и lock-файлы. Terraform state, plans, реальные tfvars,
приватные отчёты и ключи исключены `.gitignore`; их нельзя добавлять через `git add -f`.
В этой операторской копии локальные state сохранены на прежних относительных
путях внутри `environments/pz`, с правами `0600`. Три state Kubernetes остаются
на VM. Новый clone сам по себе не является готовым к apply: сначала восстановить
нужный state и проверить переменные по runbook, иначе возможны повторное создание
ресурсов и потеря управления ими.

Прежний локальный путь `../infrastructure` — ссылка на `environments/pz`; второй
копии исходников нет. Архив до переноса хранится вне репозитория в
`../operator-backups/20260930-before-repo-move/` и содержит приватные данные.
