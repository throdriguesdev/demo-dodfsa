include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env_vars    = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  region_vars = read_terragrunt_config(find_in_parent_folders("region.hcl"))
  environment = local.env_vars.locals.environment
  region      = local.region_vars.locals.region
  aws_profile = get_env("TF_VAR_aws_profile", "default")
}

dependency "eks" {
  config_path = "${get_repo_root()}/live/${local.environment}/${local.region}/compute/.terragrunt-stack/eks"

  mock_outputs = {
    cluster_name = "mock-cluster"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "eks_addons" {
  config_path = "${get_repo_root()}/live/${local.environment}/${local.region}/compute/.terragrunt-stack/eks-addons"

  mock_outputs = {
    namespace = "mock"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

generate "helm_provider" {
  path      = "helm_provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    data "aws_eks_cluster" "this" {
      name = var.cluster_name
    }

    provider "helm" {
      kubernetes {
        host                   = data.aws_eks_cluster.this.endpoint
        cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
        exec {
          api_version = "client.authentication.k8s.io/v1beta1"
          command     = "aws"
          args        = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", "${local.region}", "--profile", "${local.aws_profile}"]
        }
      }
    }
  EOF
}

terraform {
  source = "git::https://github.com/throdriguesdev/terraform-aws.git//catalog/modules/argocd?ref=v1.0.0"
}

inputs = {
  cluster_name    = dependency.eks.outputs.cluster_name
  ingress_host    = try(values.ingress_host, "argocd.lab.trdevops.com.br")
  chart_version   = try(values.chart_version, "7.7.3")
  gitops_repo_url = try(values.gitops_repo_url, "")
}
