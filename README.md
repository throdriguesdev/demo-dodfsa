# demo-dodfsa

**DevOpsDays Feira de Santana 2026** — live demo repository.

Full observability stack on Kubernetes using the open-source **LGTM stack** (Loki · Grafana · Tempo · Mimir) + Alloy, deployed GitOps-style with ArgoCD on AWS EKS.

## Architecture

```
iac/          Terragrunt IaC — provisions VPC + EKS cluster + ArgoCD
              Modules sourced from github.com/throdriguesdev/terraform-aws@v1.0.0

gitops/       ArgoCD app-of-apps — manages the full observability stack
  bootstrap/  Apply once after cluster is up to hand control to ArgoCD
  apps/       One Application YAML per component
  system/     Helm values, ClusterIssuers, ExternalSecrets
```

## Live endpoints (when cluster is up)

| Service | URL |
|---------|-----|
| Grafana | https://grafana.lab.trdevops.com.br |
| ArgoCD | https://argocd.lab.trdevops.com.br |
| Alloy UI | https://alloy.lab.trdevops.com.br |

## Stack

| Component | Role | Chart |
|-----------|------|-------|
| Loki | Log aggregation | grafana/loki 6.35.1 |
| Mimir | Metrics backend (Prometheus-compatible) | grafana/mimir-distributed 5.7.0 |
| Tempo | Distributed tracing | grafana/tempo 1.24.4 |
| Grafana | Dashboards (datasources pre-wired) | grafana/grafana 8.8.2 |
| Alloy | Collector — metrics, logs, traces | grafana/k8s-monitoring 1.6.38 |
| ArgoCD | GitOps controller | argo/argo-cd 7.7.3 |

## Prerequisites

- OpenTofu >= 1.6
- Terragrunt >= 1.0
- AWS CLI v2, kubectl, helm
- AWS account + credentials
- Route53 hosted zone (update `dns_zone` in the compute stack)

## Quick start

```bash
# 1. Configure credentials
cd iac
cp .envrc.example .envrc
direnv allow   # or: source .envrc

# 2. Bring up networking (VPC)
cd live/dev/us-east-1/networking
terragrunt stack run apply

# 3. Bring up compute (EKS + addons + ArgoCD)
cd ../compute
terragrunt stack run apply

# 4. Update kubeconfig
aws eks update-kubeconfig --name devopsdays-dev --region us-east-1 --profile $TF_VAR_aws_profile

# 5. Store Grafana admin password in AWS Secrets Manager
aws secretsmanager create-secret \
  --name /devopsdays/grafana/admin \
  --secret-string '{"username":"admin","password":"your-password"}' \
  --region us-east-1

# 6. Update the ACME email in gitops/system/cert-manager/cluster-issuer.yaml
# Then bootstrap ArgoCD — it takes over from here
kubectl apply -f gitops/bootstrap/app-of-apps.yaml

# 7. Watch ArgoCD sync the stack
kubectl port-forward svc/argocd-server -n argocd 8080:443
# open https://localhost:8080
# password: kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath="{.data.password}" | base64 -d
```

## Teardown

```bash
# Release ALBs first (otherwise destroy gets stuck on security groups)
kubectl delete ingress --all -A

cd iac/live/dev/us-east-1/compute
terragrunt stack run destroy

cd ../networking
terragrunt stack run destroy
```

## Module library

IaC modules are maintained at [throdriguesdev/terraform-aws](https://github.com/throdriguesdev/terraform-aws).
This repo pins to `v1.0.0` — update `?ref=v1.0.0` in `iac/catalog/units/*/terragrunt.hcl` to upgrade.
