# observability/

Tudo o que a **stack LGTM** (Loki, Grafana, Tempo, Mimir) e o **Grafana Alloy**
precisam para rodar está nesta pasta. Cada subpasta é a configuração de um
componente; o `docker-compose.yml` na raiz do repo monta esses caminhos dentro
dos containers correspondentes.

```
observability/
├── alloy/
│   └── config.alloy                       Pipeline única do Alloy — ver abaixo
├── grafana/
│   ├── dashboards/                        17 JSONs de dashboards prontos
│   └── provisioning/
│       ├── dashboards/dashboards.yml      File provider apontando para dashboards/
│       └── datasources/datasources.yml    Mimir + Loki + Tempo com correlações ligadas
├── loki/
│   └── loki.yml                           Loki single-binary (storage filesystem)
├── mimir/
│   ├── mimir.yml                          Mimir single-binary (filesystem, native histograms)
│   └── rules.yml                          Recording rules + alertas
├── tempo/
│   └── tempo.yml                          Tempo + metrics generator (spanmetrics + service graphs)
└── rabbitmq/
    └── enabled_plugins                    Lista de plugins do RabbitMQ (management + prometheus)
```

## Como tudo se conecta

```
      ┌──────────────────┐
      │    aplicações    │  Serviços FastAPI → SDK OTel + cliente Prometheus
      └────────┬─────────┘
               │ /metrics scrape         logs JSON em stdout      OTLP gRPC
               ▼                         ▼                       ▼
      ┌──────────────────────────────────────────────────────────────┐
      │                       Grafana Alloy                          │
      │  prometheus.scrape  +  loki.source.docker  +  otelcol.receiver │
      └──────────┬─────────────────────┬─────────────────────┬──────┘
                 │ remote_write        │ push                │ OTLP export
                 ▼                     ▼                     ▼
           ┌──────────┐          ┌──────────┐          ┌──────────┐
           │  Mimir   │          │   Loki   │          │  Tempo   │
           └────┬─────┘          └────┬─────┘          └────┬─────┘
                │                     │                     │
                │                     │   ┌─────────────────┤
                │                     │   │  metrics generator
                │                     │   │  remote_write   │
                │◄────────────────────┼───┘                 │
                │                     │                     │
                └──────────┐          │         ┌───────────┘
                           ▼          ▼         ▼
                       ┌──────────────────────────┐
                       │        Grafana           │
                       │   (dashboards + Explore) │
                       └──────────────────────────┘
```

## alloy/

O `config.alloy` é a única fonte da verdade para a camada de agente. Ele faz
três coisas:

1. **Métricas.** Blocos `prometheus.scrape` puxam `/metrics` de cada exporter
   (postgres, redis, rabbitmq, node, cadvisor) e de cada serviço da aplicação,
   e `prometheus.remote_write` empurra para o Mimir.
2. **Logs.** `loki.source.docker` faz tail do socket do Docker pegando logs de
   container. `loki.process` faz parse do JSON do structlog (`trace_id`,
   `span_id`, `level`, `customer_name`), promove `level` para label e armazena
   `trace_id` / `span_id` / `customer_name` como **structured metadata do
   Loki** — é isso que torna a correlação `logs → trace` rápida.
3. **Traces.** `otelcol.receiver.otlp` aceita OTLP gRPC (4317) e HTTP (4318)
   das aplicações, faz batch com `otelcol.processor.batch` e exporta para o
   Tempo via `otelcol.exporter.otlp`.

## grafana/

### provisioning/datasources/datasources.yml

Três datasources, pré-configurados com correlações cruzadas que tornam a demo
interessante:

- **Mimir** (compatível com Prometheus) — datasource padrão.
- **Loki** — tem uma entrada de `derivedFields` que extrai o `trace_id` da
  linha de log JSON e transforma num link clicável para o Tempo.
- **Tempo** — configurado com:
  - `tracesToLogsV2` → clicar numa span abre o Loki filtrado pelo `trace_id`
    dela.
  - `tracesToMetrics` → clicar numa span mostra painéis de request rate / error
    rate / latência p95 construídos a partir de spanmetrics
    (`traces_spanmetrics_*`).
  - `serviceMap` → habilitado, alimentado pelos dados de spanmetrics no Mimir.
  - `nodeGraph` → habilitado.

### provisioning/dashboards/dashboards.yml

Provider baseado em arquivo que observa `/var/lib/grafana/dashboards` e
recarrega a cada 10 segundos. Edições nos JSONs aparecem no Grafana sem
restart.

### dashboards/

17 dashboards cobrindo:

| Dashboard               | O que mostra                                              |
|-------------------------|-----------------------------------------------------------|
| `demo-overview.json`    | Saúde geral de toda a demo                                |
| `fastapi.json`          | Métricas RED + histogramas de latência da API             |
| `business.json`         | Volume de pedidos, receita, pagamentos — KPIs de negócio  |
| `order-lifecycle.json`  | Fluxo end-to-end do pedido: criado → processado → completo |
| `order-agent.json`      | Internals do gerador sintético de tráfego                 |
| `user-orders.json`      | Quebra por persona de cliente                             |
| `microservices.json`    | RED por serviço em todos os microsserviços                |
| `service-graph.json`    | Service graph a partir do metrics generator do Tempo      |
| `service-topology.json` | Visão de topologia com arestas ponderadas por taxa        |
| `correlation.json`      | Mostra os pulos métrica ↔ trace ↔ log num único painel    |
| `endpoint-health.json`  | Taxa de erro + latência p95 por endpoint                  |
| `payment-analytics.json`| Pagamentos por método / status                            |
| `postgres.json`         | Vindo do postgres-exporter                                |
| `redis.json`            | Vindo do redis-exporter                                   |
| `rabbitmq.json`         | Vindo do rabbitmq-exporter                                |
| `ec2-instance.json`     | Métricas de host do node-exporter / cadvisor              |
| `k6.json`               | Métricas de teste de carga do k6 (se você ligar k6 contra a API) |

## loki/

Loki single-binary com storage filesystem e structured-metadata habilitado.
Não use essa configuração em produção — troque o backend de storage e rode o
Loki em modo escalável.

## mimir/

- `mimir.yml` — Mimir single-binary com blocks no filesystem, native
  histograms habilitados. Mesma ressalva do Loki: não é config de produção.
- `rules.yml` — recording rules + alertas carregados no tenant anônimo
  (`/data/mimir/rules/anonymous/rules.yml`).

## tempo/

- Receiver OTLP gRPC em `4317`, HTTP em `4318`.
- O **metrics generator** é a peça crítica: emite `traces_service_graph_*`
  (para o service map) e `traces_spanmetrics_*` (RED por span) e faz
  remote-write para o Mimir. Essas métricas alimentam tanto a visão de service
  graph quanto os links de `tracesToMetrics`.
- Fixado em `grafana/tempo:2.6.1` — versões mais novas podem precisar de
  ajustes na config.

## rabbitmq/

Só a lista `enabled_plugins`. `rabbitmq_management` (UI em :15672) e
`rabbitmq_prometheus` (consumido pelo Alloy via exporter).

## Mudando coisas

| Mudou                                       | Faça isso                                  |
|---------------------------------------------|--------------------------------------------|
| `alloy/config.alloy`                        | `docker compose restart alloy`             |
| `loki/loki.yml` / `mimir/mimir.yml` / `tempo/tempo.yml` | `docker compose restart <serviço>` |
| `grafana/provisioning/*`                    | `docker compose restart grafana`           |
| `grafana/dashboards/*.json`                 | Recarrega sozinho em ~10s                  |
| `mimir/rules.yml`                           | `docker compose restart mimir`             |
