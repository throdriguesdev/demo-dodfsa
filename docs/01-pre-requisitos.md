# Pré-requisitos

Antes de provisionar o cluster, você precisa ter as ferramentas certas instaladas e
uma conta AWS configurada corretamente. Este documento cobre tudo que é necessário.

---

## Ferramentas necessárias

### OpenTofu >= 1.6

OpenTofu é uma alternativa open-source ao Terraform, 100% compatível com a linguagem
HCL. É o motor que cria os recursos de infraestrutura na AWS (VPC, EKS, etc.).

**macOS:**
```bash
brew install opentofu
```

**Linux:**
```bash
curl -fsSL https://get.opentofu.org/install-opentofu.sh | sh -s -- --install-method deb
# ou para RPM-based:
curl -fsSL https://get.opentofu.org/install-opentofu.sh | sh -s -- --install-method rpm
```

Confirme: `tofu --version` (deve mostrar `OpenTofu v1.6.x` ou superior)

### Terragrunt >= 1.0

Terragrunt é uma camada acima do OpenTofu/Terraform que adiciona suporte a stacks
(múltiplos módulos com dependências), redução de repetição de configuração e
gerenciamento de estado remoto. Nesse projeto, você vai rodar `terragrunt stack run
apply` em vez de `tofu apply`.

**macOS:**
```bash
brew install terragrunt
```

**Linux:**
```bash
VERSION=v1.0.1
curl -fsSL "https://github.com/gruntwork-io/terragrunt/releases/download/${VERSION}/terragrunt_linux_amd64" \
  -o /usr/local/bin/terragrunt
chmod +x /usr/local/bin/terragrunt
```

Confirme: `terragrunt --version`

### AWS CLI v2

A interface de linha de comando da AWS. Usada para configurar credenciais, atualizar
o kubeconfig e gerenciar recursos da AWS.

**macOS:**
```bash
brew install awscli
```

**Linux:**
```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
```

Confirme: `aws --version` (deve mostrar `aws-cli/2.x.x`)

### kubectl

O cliente de linha de comando do Kubernetes. Usado para interagir com o cluster EKS
após o provisionamento.

**macOS:**
```bash
brew install kubectl
```

**Linux:**
```bash
curl -fsSL "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
  -o /usr/local/bin/kubectl
chmod +x /usr/local/bin/kubectl
```

Confirme: `kubectl version --client`

### helm

Gerenciador de pacotes do Kubernetes. Usado pelo ArgoCD para instalar aplicações via
Helm charts.

**macOS:**
```bash
brew install helm
```

**Linux:**
```bash
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

Confirme: `helm version`

### git

Para clonar o repositório.

```bash
# macOS
brew install git

# Ubuntu/Debian
sudo apt install git
```

---

## Verificação das ferramentas

Execute todos esses comandos — todos devem retornar versão sem erro:

```bash
tofu --version
terragrunt --version
aws --version
kubectl version --client
helm version
git --version
```

---

## Conta AWS

### Permissões necessárias

Para uma demo, a forma mais simples é usar uma conta com permissão de
**AdministratorAccess** (a política gerenciada da AWS que dá acesso total).

O provisionamento cria: VPC com subnets públicas/privadas, NAT Gateway, cluster EKS
com node group, IAM roles para os addons (ALB controller, External DNS, External
Secrets), validação de certificado ACM, registros Route53, Security Groups e ALB
listeners.

### Criando Access Keys

1. Acesse o console AWS em https://console.aws.amazon.com/iam
2. Vá em **Users** e clique no seu usuário
3. Clique na aba **Security credentials**
4. Em "Access keys", clique em **Create access key**
5. Escolha "Command Line Interface (CLI)" e confirme
6. Copie o **Access key ID** e o **Secret access key** — você não conseguirá ver a
   secret key depois de fechar essa tela

### Configurando o profile AWS

Em vez de usar as credenciais padrão (que podem conflitar com outras contas), configure
um profile dedicado:

```bash
aws configure --profile th
```

Você vai preencher:
```
AWS Access Key ID [None]: AKIA...
AWS Secret Access Key [None]: xxxxxxxx
Default region name [None]: us-east-1
Default output format [None]: json
```

Confirme que está funcionando:
```bash
aws sts get-caller-identity --profile th
```

Deve retornar seu Account ID, User ID e ARN.

---

## Route53: hosted zone

Uma **hosted zone** no Route53 é o serviço de DNS da AWS. Você precisa de uma porque
o ALB Ingress Controller cria registros DNS automaticamente para cada Ingress do
Kubernetes — é assim que `grafana.lab.seudominio.com.br`, `argocd.lab.seudominio.com.br`,
etc., ficam acessíveis via HTTPS.

Para criar uma hosted zone:
1. Acesse https://console.aws.amazon.com/route53
2. Clique em **Hosted zones** > **Create hosted zone**
3. Digite o domínio (ex: `lab.seudominio.com.br`)
4. Escolha **Public hosted zone**
5. Copie os 4 nameservers que a AWS fornece e configure-os no seu registrador de
   domínio (GoDaddy, Registro.br, etc.)

A propagação de DNS pode levar até 48 horas, mas geralmente ocorre em menos de 1 hora.

---

## Certificado ACM: wildcard HTTPS

O ACM (AWS Certificate Manager) emite e gerencia certificados TLS/HTTPS gratuitos.
Você precisa de um certificado wildcard para que todos os subdomínios
(`*.lab.seudominio.com.br`) sejam cobertos por um único certificado.

### Solicitando o certificado

```bash
aws acm request-certificate \
  --domain-name "*.lab.seudominio.com.br" \
  --validation-method DNS \
  --region us-east-1 \
  --profile th
```

O comando retorna um ARN como
`arn:aws:acm:us-east-1:ACCOUNT_ID:certificate/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.
Guarde esse ARN — você vai precisar dele na configuração do cluster.

### Atenção ao registro CAA

Alguns domínios têm registros CAA que definem quais autoridades certificadoras podem
emitir certificados para aquele domínio. Se o seu domínio tiver um registro CAA, você
precisa incluir `amazon.com`:

```
lab.seudominio.com.br. CAA 0 issue "amazon.com"
lab.seudominio.com.br. CAA 0 issuewild "amazon.com"
```

Sem esses registros, o ACM não consegue emitir o certificado mesmo que a validação
DNS passe. Se o seu domínio não tiver registro CAA, não precisa fazer nada.

Aguarde o status do certificado mudar para `ISSUED` (geralmente 5–30 minutos):

```bash
aws acm describe-certificate \
  --certificate-arn "arn:aws:acm:us-east-1:ACCOUNT_ID:certificate/SEU-ARN" \
  --region us-east-1 \
  --profile th \
  --query 'Certificate.Status'
```

---

## Verificação final

Execute todos esses comandos antes de começar o provisionamento:

```bash
# Ferramentas
tofu --version
terragrunt --version
aws --version
kubectl version --client
helm version

# Credenciais AWS
aws sts get-caller-identity --profile th

# Route53 — listar hosted zones
aws route53 list-hosted-zones --profile th

# ACM — listar certificados emitidos
aws acm list-certificates --region us-east-1 --profile th \
  --query 'CertificateSummaryList[?Status==`ISSUED`]'
```

Se todos retornarem sem erro e mostrarem os recursos esperados, você está pronto.
Siga para [Guia de provisionamento](04-provisionamento.md).
