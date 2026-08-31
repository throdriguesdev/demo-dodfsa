terraform_binary              = "tofu"
terraform_version_constraint  = ">= 1.6"
terragrunt_version_constraint = ">= 1.0"

locals {
  account_vars = read_terragrunt_config(find_in_parent_folders("account.hcl"))
  region_vars  = read_terragrunt_config(find_in_parent_folders("region.hcl"))
  env_vars     = read_terragrunt_config(find_in_parent_folders("env.hcl"))

  account_id  = local.account_vars.locals.account_id
  region      = local.region_vars.locals.region
  environment = local.env_vars.locals.environment

  aws_profile = get_env("TF_VAR_aws_profile", "default")
}

remote_state {
  backend = "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = {
    bucket       = "tf-state-${local.account_id}"
    key          = "demo-dodfsa/${path_relative_to_include()}/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
    profile      = local.aws_profile
  }
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    provider "aws" {
      region  = "${local.region}"
      profile = "${local.aws_profile}"

      default_tags {
        tags = {
          Project     = "demo-dodfsa"
          Environment = "${local.environment}"
          ManagedBy   = "terragrunt"
        }
      }
    }
  EOF
}

catalog {
  urls = [
    "${get_repo_root()}/catalog"
  ]
}
