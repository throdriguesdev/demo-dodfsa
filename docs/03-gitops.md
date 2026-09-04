# GitOps e ArgoCD: como funciona

Antes de começar a provisionar o cluster, vale entender o modelo GitOps — ele é
diferente do jeito tradicional de fazer deploy, e entender a diferença vai evitar
muita confusão.

---

## O que é GitOps

No modelo tradicional, você roda um comando para fazer deploy: `kubectl apply`,
`helm upgrade`, etc. O estado do cluster só muda quando alguém roda um comando.

No **GitOps**, o repositório Git é a fonte da verdade. O que está no Git é o que
deve estar no cluster. Um agente rodando dentro do cluster (no nosso caso, o ArgoCD)
fica observando o repositório e aplicando automaticamente qualquer mudança que detectar.

Benefícios práticos:
- Toda mudança passa pelo Git (histórico, revisão, rollback fácil)
- Se alguém fizer uma mudança manual no cluster (`kubectl edit`), o ArgoCD reverte
  automaticamente para o que está no Git (`selfHeal`)
- Você pode destruir e recriar o cluster do zero em minutos — o ArgoCD restaura tudo

---

## Como o ArgoCD funciona

O ArgoCD é um controlador Kubernetes que gerencia objetos do tipo `Application`.
Uma `Application` define:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: grafana
  namespace: argocd
spec:
  source:
    repoURL: https://github.com/throdriguesdev/demo-dodfsa.git
    path: gitops/monitoring/grafana       # onde estão os manifests
    targetRevision: HEAD                  # qual branch/tag acompanhar
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring                 # onde instalar
  syncPolicy:
    automated:
      prune: true      # remove recursos que foram deletados do Git
      selfHeal: true   # reverte mudanças manuais no cluster
```

Quando o ArgoCD detecta que o estado do cluster é diferente do que está no Git, ele
chama isso de "OutOfSync". Com `automated sync` habilitado, ele reconcilia sozinho
sem precisar de intervenção manual.

### O que significa cada política

| Política | O que faz |
|----------|----------|
| `prune: true` | Se você deletar um recurso do Git, o ArgoCD deleta do cluster também |
| `selfHeal: true` | Se alguém alterar um recurso diretamente no cluster, o ArgoCD reverte para o que está no Git |
| `CreateNamespace: true` | O ArgoCD cria o namespace se não existir |
| `ServerSideApply: true` | Usa o mecanismo de apply do servidor Kubernetes (melhor para CRDs grandes) |

---

## O padrão App-of-Apps

Em vez de criar cada Application manualmente, usamos o padrão **App-of-Apps**: uma
Application especial que gerencia outras Applications.

```
gitops/bootstrap/app-of-apps.yaml
         |
         | é uma Application que aponta para:
         v
gitops/apps/  (diretório com recursão habilitada)
   |
   +-- monitoring/grafana.yaml        --> Application "grafana"
   +-- monitoring/loki.yaml           --> Application "loki"
   +-- monitoring/mimir.yaml          --> Application "mimir"
   +-- monitoring/tempo.yaml          --> Application "tempo"
   +-- monitoring/alloy.yaml          --> Application "alloy"
   +-- platform/cert-manager.yaml     --> Application "cert-manager-config"
   +-- platform/external-secrets.yaml --> Application "external-secrets-config"
   +-- platform/storage.yaml          --> Application "storage"
   +-- demo-app/*.yaml                --> Applications dos microsserviços
```

Você só precisa aplicar o `app-of-apps.yaml` uma vez. A partir daí, o ArgoCD descobre
e gerencia todas as outras Applications automaticamente. Se você adicionar um novo
arquivo YAML em `gitops/apps/`, o ArgoCD vai criar a Application correspondente
automaticamente na próxima sincronização.

---

## Estrutura de pastas do gitops/

```
gitops/
  bootstrap/
    app-of-apps.yaml      O ponto de entrada — aplique uma vez após o cluster subir

  apps/                   Applications gerenciadas pelo app-of-apps
    monitoring/           Uma Application por componente da stack LGTM
    platform/             Applications para infraestrutura de suporte
    demo-app/             Applications dos microsserviços

  monitoring/             Valores Helm e configurações dos componentes LGTM
    grafana/
      values.yaml         Customizações do chart do Grafana
      external-secret.yaml  Sincronização da senha admin do AWS Secrets Manager
      dashboards/         Dashboards provisionados como ConfigMaps
    loki/values.yaml
    tempo/values.yaml
    alloy/                Configuração do Alloy (pipelines de coleta)
    mimir/                Manifests do Mimir

  platform/               Configurações de infraestrutura de suporte
    cert-manager/         ClusterIssuer (Let's Encrypt)
    external-secrets/     SecretStore (AWS Secrets Manager)
    storage/              StorageClass (EBS gp3)

  demo-app/               Manifests dos microsserviços Python
```

---

## Como fazer uma mudança

O fluxo é simples:

1. Edite um arquivo no repositório (ex: aumentar o número de réplicas do Grafana em
   `gitops/monitoring/grafana/values.yaml`)
2. Commit e push para o branch `main`:

```bash
git add gitops/monitoring/grafana/values.yaml
git commit -m "feat: aumentar replicas do Grafana para 2"
git push origin main
```

3. O ArgoCD detecta a mudança em até 3 minutos (polling interval padrão) e sincroniza
   automaticamente

4. Você pode acompanhar o sync na UI do ArgoCD:
   `https://argocd.lab.seudominio.com.br`

---

## Acompanhando pelo ArgoCD UI

Acesse o ArgoCD com as credenciais:
- **Usuário:** admin
- **Senha:** `DevOpsDays2026!` (configurada na criação do cluster)
  - ou obtenha via kubectl:
    ```bash
    kubectl get secret argocd-initial-admin-secret -n argocd \
      -o jsonpath="{.data.password}" | base64 -d
    ```

Na UI você vê:
- Lista de todas as Applications com status (Synced, OutOfSync, Degraded)
- Para cada Application: os recursos Kubernetes que ela gerencia
- Histórico de deploys (quem fez o quê e quando)
- Logs de sync em tempo real

---

## Quando forçar um sync manualmente

Em situações normais, o ArgoCD sincroniza sozinho. Mas às vezes você precisa forçar:

**Via UI:** abra a Application e clique em **Sync** > **Synchronize**

**Via CLI:**
```bash
# Instalar o CLI do ArgoCD
brew install argocd

# Fazer login
argocd login argocd.lab.seudominio.com.br

# Forçar sync de uma Application específica
argocd app sync grafana

# Forçar sync de todas as Applications
argocd app sync --all
```

**Quando forçar sync faz sentido:**
- Você acabou de criar um secret no AWS Secrets Manager e quer que o External Secrets
  Operator sincronize agora (sem esperar o próximo ciclo)
- Você fez uma mudança no Git e quer ver o resultado imediatamente
- Uma Application ficou em estado `Degraded` por um erro transiente e você quer tentar novamente
