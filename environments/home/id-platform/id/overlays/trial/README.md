# Текущее закрытое размещение ID

Base applications.yaml намеренно оставляет replicas=0 для восстановления Secrets
и пробной базы. Этот overlay фиксирует фактически запущенную пробу: replicas=1,
все пять CronJobs по-прежнему suspend=true. Он не меняет DNS, TLS, Caddy gate,
содержимое базы или production-состояние облака.

```sh
sudo k3s kubectl kustomize . > /tmp/id-trial.yaml
sudo k3s kubectl apply --dry-run=server -f /tmp/id-trial.yaml
```

Применять только после восстановления пробных данных/Secrets, проверки их
совместимости и сохранения LAN-only route. Этот файл не является production
cutover и не разрешает включить cron на синтетических trial-данных.

Base хранится в `../../base`; прежний путь `id/applications.yaml` — symlink
для сохранения совместимости с генератором. Используется стандартная структура
[base/overlay Kustomize](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/#bases-and-overlays).
