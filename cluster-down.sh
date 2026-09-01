#!/usr/bin/env bash
set -euo pipefail

CLUSTER=devopsdays-dev
PROFILE=${AWS_PROFILE:-th}
REGION=us-east-1

echo "Scaling down $CLUSTER node group to 0..."
NG=$(aws eks list-nodegroups --cluster-name "$CLUSTER" --region "$REGION" --profile "$PROFILE" \
  --query 'nodegroups[0]' --output text)

aws eks update-nodegroup-config \
  --cluster-name "$CLUSTER" \
  --nodegroup-name "$NG" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --scaling-config minSize=0,maxSize=3,desiredSize=0

echo "Waiting for nodes to terminate..."
aws eks wait nodegroup-active \
  --cluster-name "$CLUSTER" \
  --nodegroup-name "$NG" \
  --region "$REGION" \
  --profile "$PROFILE"

echo "Done. Cluster control plane is still running (~\$0.10/hr)."
echo "Run ./cluster-up.sh to resume."
