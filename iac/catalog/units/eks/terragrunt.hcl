include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env_vars    = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  region_vars = read_terragrunt_config(find_in_parent_folders("region.hcl"))
  environment = local.env_vars.locals.environment
  region      = local.region_vars.locals.region
}

dependency "vpc" {
  config_path = "${get_repo_root()}/live/${local.environment}/${local.region}/networking/.terragrunt-stack/vpc"

  mock_outputs = {
    vpc_id             = "vpc-00000000000000000"
    private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

terraform {
  source = "git::https://github.com/throdriguesdev/terraform-aws.git//catalog/modules/eks?ref=v1.0.0"
}

inputs = {
  cluster_name              = values.cluster_name
  kubernetes_version        = try(values.kubernetes_version, "1.32")
  vpc_id                    = dependency.vpc.outputs.vpc_id
  subnet_ids                = dependency.vpc.outputs.private_subnet_ids
  instance_types            = try(values.instance_types, ["t3.medium"])
  capacity_type             = try(values.capacity_type, "ON_DEMAND")
  node_min_size             = try(values.node_min_size, 2)
  node_max_size             = try(values.node_max_size, 4)
  node_desired_size         = try(values.node_desired_size, 3)
  kms_key_arn               = null
  enabled_cluster_log_types = try(values.enabled_cluster_log_types, [])
  public_access_cidrs       = try(values.public_access_cidrs, ["0.0.0.0/0"])
  authentication_mode       = try(values.authentication_mode, "API_AND_CONFIG_MAP")
  enable_ebs_csi            = true
}
