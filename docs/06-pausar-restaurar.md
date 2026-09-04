# Pausar e restaurar o cluster

Manter o cluster rodando 24h por dia custa ~$7/dia. Se você não está usando, pausar
os nós EC2 reduz para ~$2.40/dia (só o EKS control plane).

---

## Entendendo os custos

| Situação | Custo estimado/dia |
|----------|--------------------|
| Cluster totalmente rodando (2 nós + NAT + ALBs) | ~$7.00 |
| Cluster pausado (nodes = 0, só control plane) | ~$2.40 |
| Cluster destruído | $0 |

Os principais componentes de custo quando rodando:
- **EKS control plane**: $0.10/hora ($2.40/dia) — sempre presente enquanto o cluster existe
- **NAT Gateway**: $0.045/hora + $0.045/GB transferido (~$1.10/dia)
- **EC2 nodes**: ~$0.096/hora cada (~$2.30/dia para 2 nós m7i-flex.large)
- **3 ALBs**: ~$0.008/hora cada + custo por LCU (~$0.60/dia)

---

## Pausar o cluster (scale nodes para 0)

Pausar significa reduzir o nodegroup para 0 instâncias. Todos os pods são encerrados,
mas o cluster continua existindo e os dados persistem nos volumes EBS.

**Via script:**
```bash
./cluster-down.sh
```

**Via AWS CLI:**
```bash
aws eks update-nodegroup-config \
  --cluster-name devopsdays-dev \
  --nodegroup-name devopsdays-dev-default \
  --scaling-config minSize=0,maxSize=3,desiredSize=0 \
  --profile th
```

A operação leva ~3–5 minutos para os nós serem removidos.

### O que acontece ao pausar

- Todos os pods são encerrados ordeiramente (graceful termination)
- Os volumes EBS (postgres, loki, mimir, tempo, etc.) **permanecem** — os dados não são perdidos
- Os registros DNS e os ALBs **permanecem** — as URLs continuam existindo, mas retornam 502
- O EKS control plane **continua rodando** (é o custo residual de $2.40/dia)
- O ArgoCD **continua configurado** mas sem nós para rodar

---

## Restaurar o cluster

**Via script:**
```bash
./cluster-up.sh
```

**Via AWS CLI:**
```bash
aws eks update-nodegroup-config \
  --cluster-name devopsdays-dev \
  --nodegroup-name devopsdays-dev-default \
  --scaling-config minSize=2,maxSize=3,desiredSize=2 \
  --profile th
```

### O que acontece ao restaurar

1. A AWS provisiona 2 novas instâncias EC2 (~3–4 minutos para ficarem `Ready`)
2. O ArgoCD detecta que há nós disponíveis e começa a reconciliar todas as Applications
3. Cada componente da stack é recriado em ordem:
   - Pods de infraestrutura (cert-manager, external-secrets, ALB controller, external-dns)
   - Stack LGTM (loki, mimir, tempo, grafana, alloy)
   - Demo-app (postgres, redis, rabbitmq, microsserviços)
4. Os volumes EBS são montados nos novos pods — os dados estão intactos
5. Os ALBs e registros DNS já existem e voltam a funcionar assim que os pods ficam healthy

**Tempo total até tudo estar funcionando: ~7–10 minutos**

### Verificando que tudo voltou

```bash
# Atualizar kubeconfig (caso necessário)
aws eks update-kubeconfig --name devopsdays-dev --region us-east-1 --profile th

# Verificar nós
kubectl get nodes

# Verificar stack de monitoramento
kubectl get pods -n monitoring

# Verificar demo-app
kubectl get pods -n demo-app

# Verificar ALBs
kubectl get ingress -A
```

---

## Destruir completamente vs pausar

| Cenário | Recomendação |
|---------|-------------|
| Palestra amanhã | Pause (restaurar leva ~10 min, todos os dados preservados) |
| Não vai usar por mais de 1 semana | Destrua (~$2.40/dia de control plane some) |
| Problemas que não consegue resolver | Destrua e provisione do zero (~20 min) |
| Compartilhando conta com alguém | Pause (mais seguro do que destruir config) |

### Destruir completamente

```bash
# Importante: deletar os Ingresses primeiro para liberar os ALBs
# (sem isso, o destroy fica preso esperando os security groups serem deletados)
kubectl delete ingress --all -A

# Aguarde ~1 minuto para os ALBs serem deletados, depois:
cd iac/live/dev/us-east-1/compute
terragrunt stack run destroy

cd ../networking
terragrunt stack run destroy
```

**Atenção:** o destroy deleta os volumes EBS junto com o cluster. Os dados do banco,
Loki, Mimir e Tempo serão perdidos. Na próxima vez que subir, o cluster começa do zero.

---

## Checklist antes de uma apresentação

- [ ] Cluster restaurado (`./cluster-up.sh`)
- [ ] Todos os nós em `Ready` (`kubectl get nodes`)
- [ ] Todos os pods em `Running` em `monitoring` e `demo-app`
- [ ] Grafana acessível com dados (logs, métricas e traces aparecendo)
- [ ] ArgoCD mostrando todas as Applications como `Synced` e `Healthy`
- [ ] Testar os três links de correlação (trace → logs, trace → métricas, log → trace)
- [ ] Gerar tráfego de teste: `curl -X POST "https://demo-api.lab.seudominio.com.br/orders/bulk?count=20"`
