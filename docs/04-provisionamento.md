# Guia de provisionamento: do zero ao cluster

Este guia cobre todos os passos para criar o cluster do zero. Tempo estimado total:
**20–30 minutos** (a maior parte é espera pela AWS).

Antes de começar, certifique-se de ter tudo em [Pré-requisitos](01-pre-requisitos.md).

---

## Passo 1: clonar o repositório e entender a estrutura

```bash
git clone https://github.com/throdriguesdev/demo-dodfsa.git
cd demo-dodfsa
```

Estrutura relevante para o provisionamento:

```
iac/
  live/dev/us-east-1/
    networking/         # VPC — provisionar primeiro
    compute/            # EKS + addons + ArgoCD — provisionar depois
gitops/
  bootstrap/
    app-of-apps.yaml    # aplicar após o cluster subir
```

---

## Passo 2: configurar credenciais e variáveis

Copie o arquivo de exemplo de variáveis de ambiente:

```bash
cp iac/.envrc.example iac/.envrc
```

Edite `iac/.envrc` com seu editor e preencha:

```bash
export AWS_PROFILE=th              # profile configurado com suas credenciais
export AWS_REGION=us-east-1
export TF_VAR_aws_profile=th
```

Ative as variáveis:
```bash
# Se você usa direnv:
cd iac && direnv allow

# Ou ative manualmente:
source iac/.envrc
```

### Ajustar variáveis do stack

Abra `iac/live/dev/us-east-1/compute/terragrunt.stack.hcl` e ajuste os valores:

```hcl
unit "argocd" {
  values = {
    ingress_host        = "argocd.lab.seudominio.com.br"  # seu domínio
    acm_certificate_arn = "arn:aws:acm:us-east-1:SEU_ACCOUNT:certificate/SEU-ARN"
  }
}
```

Abra `iac/live/dev/us-east-1/networking/terragrunt.stack.hcl` e verifique:
```hcl
unit "vpc" {
  values = {
    # Não precisa alterar para a demo padrão
  }
}
```

---

## Passo 3: provisionar a rede (VPC)

**Tempo estimado: 3–5 minutos**

```bash
cd iac/live/dev/us-east-1/networking
terragrunt stack run apply
```

O que acontece:
- Cria a VPC com CIDR `10.0.0.0/16`
- Cria 3 subnets públicas e 3 subnets privadas em diferentes Availability Zones
- Cria o Internet Gateway e o NAT Gateway
- Aplica as tags necessárias para o ALB Ingress Controller

Quando terminar, você verá:
```
Apply complete! Resources: X added, 0 changed, 0 destroyed.
```

---

## Passo 4: provisionar o cluster (EKS + addons + ArgoCD)

**Tempo estimado: 12–18 minutos**

```bash
cd ../compute
terragrunt stack run apply
```

O que acontece (em ordem):
1. **eks**: cria o control plane EKS e o node group com 2 instâncias EC2
2. **eks-addons**: instala AWS Load Balancer Controller, External DNS, External Secrets Operator, cert-manager
3. **argocd**: instala o ArgoCD e configura o Ingress

Esta é a etapa mais longa. O EKS control plane leva ~10 minutos para ficar pronto,
e os node groups levam mais ~3 minutos.

---

## Passo 5: configurar o kubeconfig

Após o cluster subir, configure o kubectl para se conectar a ele:

```bash
aws eks update-kubeconfig \
  --name devopsdays-dev \
  --region us-east-1 \
  --profile th
```

Verifique que está conectado:
```bash
kubectl get nodes
```

Deve mostrar 2 nós com status `Ready`.

---

## Passo 6: criar o secret do Grafana no AWS Secrets Manager

O Grafana precisa de uma senha de admin. Ela não fica no repositório — vai no
AWS Secrets Manager e é sincronizada para o cluster pelo External Secrets Operator.

```bash
aws secretsmanager create-secret \
  --name /devopsdays/grafana/admin \
  --secret-string '{"username":"admin","password":"SuaSenhaAqui"}' \
  --region us-east-1 \
  --profile th
```

Escolha uma senha forte. Anote — você vai usar para fazer login no Grafana.

---

## Passo 7: verificar que o ArgoCD está acessível

O ALB e o DNS levam alguns minutos para propagar. Verifique o status do Ingress:

```bash
kubectl get ingress -n argocd
```

Quando o campo `ADDRESS` mostrar um DNS do ALB, acesse o ArgoCD:

```bash
# Obter a senha inicial do ArgoCD
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath="{.data.password}" | base64 -d
```

Acesse `https://argocd.lab.seudominio.com.br` no navegador (usuário: `admin`).

---

## Passo 8: bootstrap do ArgoCD (app-of-apps)

Com o ArgoCD acessível, aplique o ponto de entrada do GitOps:

```bash
kubectl apply -f gitops/bootstrap/app-of-apps.yaml
```

O que acontece:
1. O ArgoCD lê o diretório `gitops/apps/` do repositório
2. Cria uma Application para cada arquivo YAML encontrado (loki, mimir, tempo, grafana, alloy, cert-manager, external-secrets, storage, demo-app)
3. Começa a sincronizar cada Application — instalando os Helm charts e aplicando os manifests

---

## Passo 9: acompanhar o sync

Abra o ArgoCD UI e observe as Applications sendo criadas e sincronizadas. O processo
completo leva ~5–10 minutos.

Status esperado ao final:
- Todas as Applications: **Synced** + **Healthy**
- Namespace `monitoring`: loki, mimir, tempo, grafana, alloy todos rodando
- Namespace `demo-app`: todos os 8 microsserviços rodando

Você também pode acompanhar via kubectl:

```bash
# Ver status dos pods de monitoramento
kubectl get pods -n monitoring

# Ver status dos pods da demo-app
kubectl get pods -n demo-app

# Ver todas as Applications do ArgoCD
kubectl get applications -n argocd
```

---

## Passo 10: verificar os endpoints

Após o sync completo, verifique que os endpoints estão respondendo:

```bash
curl -I https://grafana.lab.seudominio.com.br
curl -I https://argocd.lab.seudominio.com.br
curl -I https://alloy.lab.seudominio.com.br
```

Todos devem retornar `HTTP/2 200` ou `HTTP/2 302`.

---

## Passo 11: fazer login no Grafana e verificar datasources

Acesse `https://grafana.lab.seudominio.com.br` com as credenciais que você definiu
no Passo 6.

Verifique os datasources em **Configuration > Data Sources**:
- **Mimir** (Prometheus-compatible) — deve mostrar "Data source connected and labels found"
- **Loki** — deve mostrar "Data source connected and labels found"
- **Tempo** — deve mostrar "Data source is working"

Se algum datasource mostrar erro, verifique os logs do pod correspondente:

```bash
kubectl logs -n monitoring -l app.kubernetes.io/name=grafana --tail=50
```

---

## Problemas comuns

**ALB não provisiona (sem ADDRESS no Ingress)**
- Verifique se o AWS Load Balancer Controller está rodando: `kubectl get pods -n kube-system | grep aws-load-balancer`
- Verifique os logs: `kubectl logs -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller`

**DNS não resolve**
- Verifique se o External DNS está rodando: `kubectl get pods -n kube-system | grep external-dns`
- DNS pode levar até 5 minutos para propagar

**Grafana em CrashLoopBackOff**
- Verifique se o secret foi criado: `kubectl get secret grafana-admin-secret -n monitoring`
- Se não existir, o External Secrets Operator não sincronizou ainda — aguarde ou veja os logs: `kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets`

**Certificado ACM não valida**
- Verifique se o registro CAA do domínio inclui `amazon.com` (veja [Pré-requisitos](01-pre-requisitos.md#atenção-ao-registro-caa))
- Verifique se os registros CNAME de validação foram criados no Route53
