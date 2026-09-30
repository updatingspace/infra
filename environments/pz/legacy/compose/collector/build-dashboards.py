#!/usr/bin/env python3
"""Generate Monium UI JSON (Settings → JSON). No credentials or API writes."""
import argparse
import json
import pathlib

p = argparse.ArgumentParser()
p.add_argument('--project', default='folder__b1gidr45ifb2c25bco2d')
p.add_argument('--only', choices=['overview', 'game', 'infrastructure'], help='Regenerate only the selected historical UI-format template.')
args = p.parse_args()

def sel(service, name, **labels):
    return '{' + ','.join(f'{k}={json.dumps(v)}' for k,v in dict(project=args.project, cluster='pz-b42', service=service, name=name, **labels).items()) + '}'
def game(name, **labels): return sel('zomboid', name, **labels)
def host(name, **labels): return sel('pz-host', name, **labels)
def kubernetes(name): return sel('pz-kubernetes', name)
def collector(name): return sel('otel-collector', name)
def rate(q): return f'non_negative_derivative({q})'
def alias(q, label): return f'alias({q}, {json.dumps(label,ensure_ascii=False)})'
def total(q): return f'series_sum({q})'

tps = alias(rate(game('storm_server_tick_total')), 'TPS')
target = alias(game('storm_server_lock_fps'), 'Цель TPS')
tick = alias(f"1000 * {total(rate(game('storm_server_tick_duration_seconds.sum')))} / {total(rate(game('storm_server_tick_duration_seconds.count')))}", 'Средний тик')
cpu = alias(f"100 * {total(rate(host('system.cpu.time',state='user|system|nice|interrupt|softirq|steal')))} / {total(host('system.cpu.logical.count'))}", 'CPU без iowait')
disk = alias('100 * '+host('system.filesystem.utilization',mountpoint='/'), 'Занято /')
environment_mountpoints = '/|/srv/pz-storage/zomboid|/srv/pz-storage/edge|/srv/pz-storage/observability'
environment_disks = alias('100 * '+host('system.filesystem.utilization',mountpoint=environment_mountpoints), '{{mountpoint}}')
heap = alias(game('jvm_memory_used_bytes',area='heap'),'Heap used')
heapmax = alias(game('jvm_memory_max_bytes',area='heap'),'Heap max')
up = alias(game('up'),'Экспортёр игры')
panel = alias('series_max('+sel('pz-panel','httpcheck.status',**{'http.status_class':'2xx'})+')','Панель HTTP 2xx')
players = alias(game('game',parameter='players'),'Игроки')
free = alias(host('system.filesystem.usage',mountpoint='/',state='free'),'Свободно /')
queue = alias(collector('otelcol_exporter_queue_size'),'Очередь')

# title, unit, queries, presentation (optional). Each chart keeps a single unit.
specs = {
 'overview': ('01 · PZ — состояние сервера', 'Быстрый обзор. 1 = проверка успешна. Экспортёр игры и HTTP-панель не заменяют проверку входа игрока. TPS сравнивается с настроенной целью. Пропуски остаются пропусками.', [
   ('Доступность','COUNT',[up,panel],'tile'),
   ('Игроки онлайн','COUNT',[players],'tile'),
   ('Свободное место /','BYTES_IEC',[free],'tile'),
   ('TPS — фактический и целевой','COUNTS_PER_SECOND',[tps,target]),
   ('Средняя длительность тика','MILLISECONDS',[tick,alias('1000 * '+game('storm_server_tick_interval_seconds'),'Целевой интервал')]),
   ('CPU хоста','PERCENT',[cpu]),
   ('Заполнение диска /','PERCENT',[disk]),
   ('JVM heap','BYTES_IEC',[heap,heapmax]),
   ('Доставка метрик: очередь','COUNT',[queue]),
 ]),
 'game': ('02 · PZ — игра и JVM', 'TPS считается из счётчика тиков. Средняя длительность = скорость суммы / скорость количества. p95/p99 не показываются: текущий экспортёр отдаёт только bucket +Inf. CPU процесса: 100% = одно ядро.', [
   ('TPS','COUNTS_PER_SECOND',[tps,target]),
   ('Средний тик','MILLISECONDS',[tick]),
   ('Игроки онлайн','COUNT',[players]),
   ('CPU процесса игры','PERCENT',[alias('100 * '+rate(game('process_cpu_seconds_total')),'CPU игры')]),
   ('JVM heap','BYTES_IEC',[heap,alias(game('jvm_memory_committed_bytes',area='heap'),'Heap committed'),heapmax]),
   ('Память процесса RSS','BYTES_IEC',[alias(game('process_resident_memory_bytes'),'RSS')]),
   ('Потоки JVM','COUNT',[alias(game('jvm_threads_current'),'Потоки'),alias(game('jvm_threads_deadlocked'),'Deadlocks')]),
   ('GC — затраты времени, s/s','SECONDS',[alias(rate(game('jvm_gc_collection_seconds.sum')),'{{gc}}')]),
   ('Объекты мира','COUNT',[alias(game('game',parameter='zombies-loaded|zombies-simulated|animals-instances|loaded-cells'),'{{parameter}}')]),
 ]),
 'infrastructure': ('03 · PZ — хост, Kubernetes и доставка', 'CPU контейнеров — текущее число занятых ядер × 100%; 100% = одно ядро. Память — container.memory.usage. Время работы контейнера показано в секундах; счётчик перезапусков kubelet summary не предоставляет. Память хоста показана отдельными рядами без суммирования. Заполнение: / и отдельные файловые системы /srv/pz-storage/{zomboid,edge,observability}; пределы окружений 26 GiB, 256 MiB и 512 MiB соответственно. Физический I/O — устройство vda. Ошибки и счётчики — за секунду.', [
   ('CPU хоста','PERCENT',[cpu]),
   ('Load average','COUNT',[alias(host('system.cpu.load_average.1m'),'1 min'),alias(host('system.cpu.load_average.5m'),'5 min'),alias(host('system.cpu.logical.count'),'Логические CPU')]),
   ('Память хоста','BYTES_IEC',[alias(host('system.memory.usage',state='used|free|cached'),'{{state}}')]),
   ('Файловые системы — занято','PERCENT',[environment_disks]),
   ('Диск vda — чтение и запись','BYTES_IEC_PER_SECOND',[alias(rate(host('system.disk.io',device='vda')),'{{direction}}')]),
   ('Диск vda — IOPS','IO_OPERATIONS_PER_SECOND',[alias(rate(host('system.disk.operations',device='vda')),'{{direction}}')]),
   ('CPU контейнеров Kubernetes','PERCENT',[alias('100 * '+kubernetes('container.cpu.usage'),'{{k8s.namespace.name}} / {{k8s.pod.name}} / {{k8s.container.name}}')]),
   ('Память контейнеров Kubernetes','BYTES_IEC',[alias(kubernetes('container.memory.usage'),'{{k8s.namespace.name}} / {{k8s.pod.name}} / {{k8s.container.name}}')]),
   ('Время работы контейнеров','SECONDS',[alias(kubernetes('container.uptime'),'{{k8s.namespace.name}} / {{k8s.pod.name}} / {{k8s.container.name}}')],'tile'),
   ('Отправка точек метрик','COUNTS_PER_SECOND',[alias(rate(collector('otelcol_exporter_sent_metric_points')),'Доставлено / s')]),
   ('Очередь доставки','COUNT',[queue,alias(collector('otelcol_exporter_queue_capacity'),'Ёмкость')]),
   ('Ошибки сбора / отказы','COUNTS_PER_SECOND',[alias(total(rate(collector('otelcol_scraper_errored_metric_points'))),'Ошибки сбора'),alias(total(rate(collector('otelcol_receiver_refused_metric_points'))),'Отказы приёма')]),
 ])
}

def build(key, title, description, panels):
    widgets=[{'position':{'x':0,'y':0,'w':36,'h':3},'text':{'text':description}}]
    for i,panel in enumerate(panels):
        name,unit,queries,*kind=panel
        ident=f'pz-{key}-{i}'
        vis={'type':'VISUALIZATION_TYPE_TILES' if kind else 'VISUALIZATION_TYPE_LINE',
             'aggregation':'SERIES_AGGREGATION_LAST','interpolate':'INTERPOLATE_LEFT',
             'yaxisSettings':{'left':{'type':'YAXIS_TYPE_LINEAR','unitFormat':'UNIT_'+unit,'min':'0'}},
             'colorSchemeSettings':{'automatic':{}}}
        if kind: vis['tilesSettings']={'showTitle':True,'showValue':True,'showSparkline':True}
        chart={'id':ident,'title':name,'displayLegend':True,'dataSources':[{'id':'monium','type':'monitoring','downsampling':{'maxPoints':1000,'gridAggregation':'GRID_AGGREGATION_AVG','gapFilling':'GAP_FILLING_NULL'}}],
               'targets':[{'type':'monitoring','dataSourceId':'monium','query':q,'name':chr(65+j),'textMode':True,'hidden':False} for j,q in enumerate(queries)],
               'seriesOverrides':[],'visualizationSettings':vis}
        widgets.append({'position':{'x':i%3*12,'y':3+i//3*8,'w':12,'h':8},'multiSourceChart':chart})
    return {'title':title,'description':description,'parametrization':None,'eventSources':None,'widgets':widgets,'presetItems':[]}

out=pathlib.Path(__file__).parent/'dashboards'
out.mkdir(exist_ok=True)
for key,(title,description,panels) in specs.items():
    if args.only and key != args.only:
        continue
    data=build(key,title,description,panels)
    path=out/(key+'.json')
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(path, len(panels), 'charts')
