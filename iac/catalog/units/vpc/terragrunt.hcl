include "root" {
  path = find_in_parent_folders("root.hcl")
}

terraform {
  source = "git::https://github.com/throdriguesdev/terraform-aws.git//catalog/modules/vpc?ref=v1.0.0"
}

inputs = {
  name            = values.name
  cidr            = try(values.cidr, "10.0.0.0/16")
  azs             = try(values.azs, ["us-east-1a", "us-east-1b", "us-east-1c"])
  public_subnets  = try(values.public_subnets, ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"])
  private_subnets = try(values.private_subnets, ["10.0.10.0/24", "10.0.11.0/24", "10.0.12.0/24"])
  enable_nat      = try(values.enable_nat, true)
  single_nat      = try(values.single_nat, true)

  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
  }
}
