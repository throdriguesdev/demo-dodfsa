# Arquitetura

Este documento explica como as duas camadas do projeto funcionam juntas: a camada de
infraestrutura (Terragrunt) que provisiona os recursos AWS, e a camada de aplicação
(ArgoCD) que gerencia o que roda dentro do cluster.

---

## Visão geral: duas camadas

```
+----------------------------------------------------------------------+
|  Camada 1: IaC (Terragrunt + OpenTofu)                               |
|                                                                      |
|  Você executa uma vez (ou quando a infra muda)                       |
|                                                                      |
|  iac/live/dev/us-east-1/networking/  -->  VPC + subnets + NAT GW    |
|  iac/live/dev/us-east-1/compute/    -->  EKS + addons + ArgoCD       |
|                                                                      |
+----------------------------------------------------------------------+
                              |
                              | cria e configura
                              v
+----------------------------------------------------------------------+
|  AWS Account                                                         |
|                                                                      |
|  VPC (10.0.0.0/16)                                                   |
|   +-- Subnets públicas  --> ALB (load balancer externo)              |
|   +-- Subnets privadas  --> EKS Node Group (EC2 m7i-flex.large x2)  |
|                                                                      |
|  EKS Cluster: devopsdays-dev (Kubernetes 1.34)                       |
|   +-- namespace: argocd          --> controlador GitOps              |
|   +-- namespace: monitoring      --> LGTM stack (Loki, Grafana,      |
|   |                                   Tempo, Mimir, Alloy)           |
|   +-- namespace: demo-app        --> 8 microsserviços Python         |
|   +-- namespace: cert-manager    --> TLS automático                  |
|   +-- namespace: external-secrets --> sincronização de secrets AWS   |
|                                                                      |
+----------------------------------------------------------------------+
                              |
                              | ArgoCD observa o repositório GitHub
                              v
+----------------------------------------------------------------------+
|  Camada 2: GitOps (ArgoCD)                                           |
|                                                                      |
|  gitops/bootstrap/app-of-apps.yaml  --> Application raiz            |
|  gitops/apps/                       --> Uma Application por serviço  |
|                                                                      |
|  ArgoCD sincroniza continuamente o estado do cluster com o que       |
|  está no repositório. Você muda um arquivo no Git, o ArgoCD aplica.  |
|                                                                      |
+----------------------------------------------------------------------+
```

---

## O que cada módulo Terraform faz

Os módulos são definidos em `iac/catalog/units/` e instanciados em
`iac/live/dev/us-east-1/`. O código fonte dos módulos está no repositório
[throdriguesdev/terraform-aws](https://github.com/throdriguesdev/terraform-aws).

### vpc

Cria a rede base do projeto:
- VPC com o CIDR configurado (`10.0.0.0/16`)
- 3 subnets públicas (onde ficam o ALB e o NAT Gateway)
- 3 subnets privadas (onde ficam os nós do EKS)
- Internet Gateway para as subnets públicas
- NAT Gateway para que os nós privados possam acessar a internet (para pulls de imagem, etc.)
- Tags necessárias para que o ALB Ingress Controller identifique as subnets corretas

### eks

Cria o cluster EKS:
- EKS control plane gerenciado pela AWS
- Node group com instâncias EC2 `m7i-flex.large` (configurável)
- IAM role para os nós com as permissões necessárias
- OIDC provider (necessário para que pods assumam IAM roles via IRSA)
- Security groups para comunicação cluster ↔ nós

### eks-addons

Instala os addons via Helm dentro do cluster EKS:
- **AWS Load Balancer Controller** — cria ALBs automaticamente para cada Ingress do Kubernetes
- **External DNS** — cria registros DNS no Route53 automaticamente para cada Ingress
- **External Secrets Operator** — sincroniza secrets do AWS Secrets Manager para Kubernetes Secrets
- **cert-manager** — gerencia certificados TLS (via Let's Encrypt ou ACM)

Cada addon tem uma IAM role dedicada criada via IRSA (IAM Roles for Service Accounts)
— o pod tem apenas as permissões que precisa, sem credenciais estáticas.

### argocd

Instala o ArgoCD no namespace `argocd`:
- Deployment do ArgoCD via Helm chart
- Configuração do Ingress para acesso via HTTPS
- Configuração do domínio via variável `dns_zone`

---

## O que cada ArgoCD Application gerencia

Após o bootstrap (`kubectl apply -f gitops/bootstrap/app-of-apps.yaml`), o ArgoCD
lê o diretório `gitops/apps/` e cria uma Application para cada arquivo YAML:

### monitoring/

| Application | O que instala |
|-------------|--------------|
| `loki.yaml` | Loki via Helm chart `grafana/loki` v6.35.1 — armazena logs |
| `mimir.yaml` | Mimir via manifests customizados — armazena métricas |
| `tempo.yaml` | Tempo via Helm chart `grafana/tempo` v1.24.4 — armazena traces |
| `grafana.yaml` | Grafana v12.3.1 via Helm chart `grafana/grafana` v10.5.15 — UI de dashboards |
| `alloy.yaml` | Grafana Alloy via `grafana/k8s-monitoring` v1.6.38 — coleta métricas e logs de todos os pods |

### platform/

| Application | O que instala |
|-------------|--------------|
| `cert-manager.yaml` | Configuração do ClusterIssuer (Let's Encrypt) |
| `external-secrets.yaml` | SecretStore apontando para o AWS Secrets Manager |
| `storage.yaml` | StorageClass para volumes persistentes (EBS gp3) |

### demo-app/

Applications que instalam os 8 microsserviços Python da demo de e-commerce.

---

## Networking: como o HTTPS chega no Grafana

```
Internet
   |
   | HTTPS :443
   v
Route53 (grafana.lab.trdevops.com.br)
   |
   | registro A criado automaticamente pelo External DNS
   v
ALB (criado automaticamente pelo AWS Load Balancer Controller)
   |
   | HTTP (TLS termina no ALB com o certificado ACM wildcard)
   v
Kubernetes Service (grafana.monitoring.svc.cluster.local)
   |
   v
Grafana Pods
```

O fluxo é totalmente automático: quando o ArgoCD faz deploy do Ingress do Grafana com
a annotation `kubernetes.io/ingress.class: alb`, o AWS Load Balancer Controller cria o
ALB com o certificado ACM configurado. O External DNS lê o hostname do Ingress e cria
o registro A no Route53 apontando para o ALB.

---

## Secrets: como as senhas chegam no cluster

O Grafana precisa de uma senha de admin. Essa senha não fica no repositório Git
(nunca faça commit de secrets). O fluxo é:

```
AWS Secrets Manager
  /devopsdays/grafana/admin --> {"username": "admin", "password": "sua-senha"}
            |
            | sincronização automática via External Secrets Operator
            v
Kubernetes Secret (grafana-admin-secret, namespace monitoring)
            |
            | montado como variável de ambiente
            v
Grafana Pod
```

O External Secrets Operator roda no cluster e sincroniza os secrets automaticamente.
Se você alterar o secret no AWS Secrets Manager, o Operator detecta a mudança e
atualiza o Kubernetes Secret.

---

## Estimativa de custo com o cluster rodando

Valores aproximados para a região `us-east-1`:

| Recurso | Custo/hora | Custo/dia |
|---------|------------|-----------|
| EKS Control Plane | $0.10 | $2.40 |
| 2x EC2 m7i-flex.large | ~$0.096 | ~$2.30 |
| NAT Gateway | ~$0.045 + transferência | ~$1.10 |
| 3x ALBs (grafana, argocd, alloy) | ~$0.008 cada + LCU | ~$0.60 |
| EBS volumes | ~$0.10/GB/mês | ~$0.50 |
| **Total idle** | **~$0.30/hora** | **~$7.00/dia** |

Os maiores custos são o NAT Gateway (cobra por hora E por GB transferido) e os ALBs.

Para reduzir custos:
- **Pausar o cluster** (scale nodes para 0) reduz para ~$2.40/dia (só o control plane)
- **Destruir completamente** quando não estiver usando (o provisionamento leva ~15 min)
- **Usar instâncias menores** (t3.medium) reduz o custo de EC2

Veja [Pausar e restaurar o cluster](06-pausar-restaurar.md) para instruções detalhadas.
