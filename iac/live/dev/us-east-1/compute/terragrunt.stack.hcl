locals {
  units_path = "${get_repo_root()}/catalog/units"
}

unit "eks" {
  source = "${local.units_path}/eks"
  path   = "eks"
  values = {
    cluster_name       = "devopsdays-dev"
    kubernetes_version = "1.32"
    instance_types     = ["t3.medium"]
    capacity_type      = "SPOT"
    node_min_size      = 2
    node_max_size      = 4
    node_desired_size  = 3
    authentication_mode = "API_AND_CONFIG_MAP"
  }
}

unit "eks-addons" {
  source = "${local.units_path}/eks-addons"
  path   = "eks-addons"
  values = {
    dns_zone = "lab.trdevops.com.br"

    enable_aws_load_balancer_controller = true
    enable_cert_manager                 = true
    enable_external_dns                 = true
    enable_external_secrets             = true
  }
}

unit "argocd" {
  source = "${local.units_path}/argocd"
  path   = "argocd"
  values = {
    ingress_host    = "argocd.lab.trdevops.com.br"
    gitops_repo_url = "https://github.com/throdriguesdev/demo-dodfsa"
  }
}
