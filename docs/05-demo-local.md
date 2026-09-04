# Rodando localmente com Docker (sem AWS)

Antes de provisionar o cluster EKS, é altamente recomendado rodar a demo localmente
com Docker Compose. Você vai entender os conceitos sem custo algum, e a transição
para o EKS vai fazer muito mais sentido.

O setup local está na raiz deste mesmo repositório — não precisa clonar nada extra.

---

## Início rápido local

```bash
# Na raiz do repositório:
docker compose up -d --build
```

Aguarde ~2 minutos e acesse:

| URL | O que é |
|-----|---------|
| http://localhost:3000 | Grafana (admin / admin) |
| http://localhost:8080/docs | API — Swagger interativo |
| http://localhost:8080/metrics | Métricas Prometheus (com exemplars) |
| http://localhost:12345 | UI do Alloy |
| http://localhost:15672 | RabbitMQ management (guest / guest) |

O `order-agent` gera pedidos automaticamente — os dashboards ficam vivos sem
interação manual. Para gerar tráfego extra:

```bash
curl -X POST http://localhost:8080/orders
curl -X POST "http://localhost:8080/orders/bulk?count=20"
```

---

## O que está incluído no setup local

```
docker-compose.yml        Todos os serviços em um arquivo
observability/
  alloy/config.alloy      Pipeline Alloy: scrape métricas, tail logs, recebe OTLP
  grafana/
    provisioning/         Datasources (Mimir, Loki, Tempo) + correlações pré-wired
    dashboards/           17 dashboards prontos
  loki/loki.yml           Loki single-binary
  mimir/mimir.yml         Mimir single-binary (native histograms)
  tempo/tempo.yml         Tempo + metrics generator (spanmetrics + service graphs)
services/
  api/                    API principal (FastAPI)
  worker/                 Processador assíncrono (consumer RabbitMQ)
  notification/           Microsserviço de notificações
  payment-gateway/        Microsserviço de pagamento
  inventory/              Microsserviço de estoque (Redis)
  fraud/                  Microsserviço de detecção de fraude
  shipping/               Microsserviço de envio
  order-agent/            Gerador autônomo de pedidos
```

---

## Local vs EKS: tabela comparativa

| Aspecto | Docker local | EKS |
|---------|-------------|-----|
| **Custo** | Gratuito (CPU/RAM local) | ~$7/dia rodando, ~$2.40/dia pausado |
| **Tempo de setup** | 5 minutos | 20–30 minutos |
| **Requisitos** | Docker Desktop | Conta AWS + domínio + ferramentas |
| **Kubernetes** | Não usa | EKS 1.34 gerenciado pela AWS |
| **Persistência** | Docker volumes locais | EBS (persiste com pods reiniciados) |
| **HTTPS** | Não (HTTP simples) | Sim (ALB + ACM wildcard) |
| **Domínio público** | Não (localhost) | Sim (`*.lab.seudominio.com.br`) |
| **GitOps** | Não (compose manual) | Sim (ArgoCD + app-of-apps) |
| **Secrets** | Env vars no compose | AWS Secrets Manager + External Secrets |
| **Coleta de traces** | Alloy → Tempo | Apps → Tempo direto (OTLP gRPC) |
| **Coleta de logs** | Alloy tail do Docker socket | Alloy via k8s-monitoring |
| **Dashboards** | 17 dashboards, reload automático | 17 dashboards via ConfigMap no GitOps |
| **Ideal para** | Aprender, explorar, apresentar | Demo profissional, simular produção |

---

## O que é idêntico nos dois ambientes

A **aplicação e a instrumentação são as mesmas** — o código dos serviços não muda:

- Os 8 microsserviços Python com o mesmo código-fonte em `services/`
- As mesmas métricas Prometheus com exemplars
- O mesmo contexto de trace propagado pelo RabbitMQ
- Os mesmos logs JSON com `trace_id` e `span_id`
- Os mesmos dashboards do Grafana
- As mesmas correlações trace ↔ log ↔ métrica

A diferença está em **quem coleta e onde roda**: localmente o Alloy usa o socket do
Docker; no EKS o k8s-monitoring lê os logs e métricas dos pods via Kubernetes.

---

## Recomendação: comece pelo local

1. `docker compose up -d --build` na raiz do repo
2. Leia [docs/01-conceitos.md](01-conceitos.md) para entender logs, métricas e traces
3. Siga o [tour guiado pelo Grafana](03-explorando.md)
4. Quando se sentir confortável com os conceitos, siga para [Guia de provisionamento](04-provisionamento.md)
