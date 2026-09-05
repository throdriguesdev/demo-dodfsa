#!/usr/bin/env bash
set -euo pipefail

CLUSTER=devopsdays-dev
PROFILE=${AWS_PROFILE:-th}
REGION=us-east-1

echo "Scaling up $CLUSTER node group to 3 (one per AZ — stack has EBS volumes in 1a/1b/1c)..."
NG=$(aws eks list-nodegroups --cluster-name "$CLUSTER" --region "$REGION" --profile "$PROFILE" \
  --query 'nodegroups[0]' --output text)

aws eks update-nodegroup-config \
  --cluster-name "$CLUSTER" \
  --nodegroup-name "$NG" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --scaling-config minSize=3,maxSize=3,desiredSize=3

echo "Waiting for nodes to be ready..."
aws eks wait nodegroup-active \
  --cluster-name "$CLUSTER" \
  --nodegroup-name "$NG" \
  --region "$REGION" \
  --profile "$PROFILE"

echo "Nodes up. Waiting for ArgoCD to reconcile (~2 min)..."
sleep 30

kubectl --context "arn:aws:eks:$REGION:075472844803:cluster/$CLUSTER" \
  get applications -n argocd 2>/dev/null || true

echo ""
echo "Stack is coming up. Check ArgoCD at https://argocd.lab.trdevops.com.br"
echo "Grafana at https://grafana.lab.trdevops.com.br"
