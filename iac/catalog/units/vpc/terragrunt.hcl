include "root" {
  path = find_in_parent_folders("root.hcl")
}

terraform {
  source = "git::https://github.com/throdriguesdev/terraform-aws.git//catalog/modules/vpc?ref=v1.0.0"
}

inputs = {
  name       = values.name
  cidr_block = try(values.cidr_block, "10.0.0.0/16")
}
