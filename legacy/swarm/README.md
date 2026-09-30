# Предыдущая конфигурация Docker Swarm

Сохранена из исходного `updatingspace/infra` (commit `bc031eb`) для сверки и
восстановления истории. Traefik, Prometheus и Grafana не относятся к новой
конфигурации PZ в `environments/pz`. Их состояние на других машинах не проверялось.

`ci-cd.yml.disabled` — прежний workflow, который выполнял `docker stack deploy`
при push в master. Он перенесён из `.github/workflows`, поэтому эта локальная
версия репозитория не запускает устаревший деплой после публикации. Сам GitHub
и работающие сервисы этим переносом не изменены. Не возвращать workflow без
проверки целевой VM и явного решения о Docker Swarm.
