# demo-dodfsa

Stack completa de observabilidade no Kubernetes usando o **LGTM stack open-source**
(Loki · Grafana · Tempo · Mimir) + Alloy, implantada via GitOps com ArgoCD em um
cluster AWS EKS — criado para a palestra do **DevOpsDays Feira de Santana 2026**.

> **Recomendado para iniciantes:** antes de provisionar o cluster na AWS, comece pelo
> setup local. Rode `docker compose up -d --build` na raiz do repo e explore os conceitos
> sem custo — depois siga para o EKS quando estiver confortável.

---

## O que você vai encontrar aqui

```
docker-compose.yml   Setup local completo — LGTM stack + app (sem AWS)
observability/       Configurações do Loki, Mimir, Tempo, Grafana e Alloy (local)
services/            Código-fonte dos 8 microsserviços Python

iac/                 Terragrunt — provisiona VPC + EKS + ArgoCD na AWS
                     Módulos sourced de github.com/throdriguesdev/terraform-aws

gitops/              ArgoCD app-of-apps — gerencia toda a stack via GitOps
  bootstrap/         Aplicar uma vez para entregar o controle ao ArgoCD
  apps/              Um Application YAML por componente
  monitoring/        Helm values e configs da stack LGTM
  demo-app/          Manifests dos 8 microsserviços Python

docs/                Documentação em português para todos os níveis
```

---

## Arquitetura

```
Você (Git push)
      |
      | ArgoCD observa o repositório
      v
+------------------------------------+
|  AWS EKS: devopsdays-dev           |
|                                    |
|  [monitoring]                      |
|    Loki   Mimir   Tempo   Grafana  |
|    Alloy (k8s-monitoring)          |
|                                    |
|  [demo-app]                        |
|    api  worker  fraud  inventory   |
|    payment  notification  shipping |
|    order-agent  postgres  redis    |
|    rabbitmq                        |
|                                    |
+------------------------------------+
      |
      | ALB + Route53 + ACM (HTTPS)
      v
   Internet
```

---

## Endpoints (quando o cluster está rodando)

| Serviço | URL |
|---------|-----|
| Grafana | https://grafana.lab.trdevops.com.br |
| ArgoCD | https://argocd.lab.trdevops.com.br |
| Alloy UI | https://alloy.lab.trdevops.com.br |
| Demo API | https://demo-api.lab.trdevops.com.br |

---

## Stack

| Componente | Função | Chart |
|-----------|--------|-------|
| Loki | Armazenamento de logs | grafana/loki 6.35.1 |
| Mimir | Backend de métricas (compatível com Prometheus) | grafana/mimir-distributed 5.7.0 |
| Tempo | Distributed tracing | grafana/tempo 1.24.4 |
| Grafana | Dashboards (datasources pré-conectados) | grafana/grafana 10.5.15 (v12.3.1) |
| Alloy | Coletor — métricas e logs do cluster | grafana/k8s-monitoring 1.6.38 |
| ArgoCD | Controlador GitOps | argo/argo-cd 7.7.3 |

---

## Documentação

| Documento | O que cobre |
|-----------|------------|
| [Pré-requisitos](docs/01-pre-requisitos.md) | Ferramentas, conta AWS, domínio, certificado ACM |
| [Arquitetura](docs/02-arquitetura.md) | Como as duas camadas (IaC + GitOps) funcionam juntas |
| [GitOps e ArgoCD](docs/03-gitops.md) | O modelo GitOps, App-of-Apps, como fazer mudanças |
| [Guia de provisionamento](docs/04-provisionamento.md) | Passo a passo do zero ao cluster rodando |
| [Rodando localmente](docs/05-demo-local.md) | Versão Docker sem AWS (recomendado para começar) |
| [Pausar e restaurar](docs/06-pausar-restaurar.md) | Economia de custos, scripts de pause/resume |

---

## Início rápido

Para quem já tem os pré-requisitos instalados:

```bash
# 1. Configurar credenciais
cd iac && cp .envrc.example .envrc
# edite .envrc com seu AWS profile e region
direnv allow

# 2. VPC (3–5 min)
cd live/dev/us-east-1/networking
terragrunt stack run apply

# 3. EKS + ArgoCD (12–18 min)
cd ../compute
terragrunt stack run apply

# 4. Configurar kubectl
aws eks update-kubeconfig --name devopsdays-dev --region us-east-1 --profile th

# 5. Criar secret do Grafana
aws secretsmanager create-secret \
  --name /devopsdays/grafana/admin \
  --secret-string '{"username":"admin","password":"SuaSenha"}' \
  --region us-east-1 --profile th

# 6. Bootstrap do ArgoCD
kubectl apply -f gitops/bootstrap/app-of-apps.yaml

# 7. Acompanhar o sync
# Acesse https://argocd.lab.seudominio.com.br
```

Para o guia detalhado com explicações de cada passo, veja
[docs/04-provisionamento.md](docs/04-provisionamento.md).

---

## Pausar e restaurar

```bash
# Pausar (escala nodes para 0 — economiza ~$4.60/dia)
./cluster-down.sh

# Restaurar
./cluster-up.sh
```

O ArgoCD recria tudo automaticamente quando os nós voltam. Dados dos volumes EBS
são preservados.

---

## Teardown completo

```bash
# Deletar Ingresses primeiro (libera os ALBs e evita que o destroy trave)
kubectl delete ingress --all -A

# Destruir compute
cd iac/live/dev/us-east-1/compute
terragrunt stack run destroy

# Destruir rede
cd ../networking
terragrunt stack run destroy
```

---

## Módulos de infraestrutura

Os módulos Terraform estão em
[throdriguesdev/terraform-aws](https://github.com/throdriguesdev/terraform-aws).
Este repo usa `v1.0.2` — atualize `?ref=v1.0.2` nos arquivos `terragrunt.hcl` dos
units para usar uma versão diferente.
