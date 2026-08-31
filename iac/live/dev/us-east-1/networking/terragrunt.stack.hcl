locals {
  units_path = "${get_repo_root()}/catalog/units"
}

unit "vpc" {
  source = "${local.units_path}/vpc"
  path   = "vpc"
  values = {
    name       = "devopsdays"
    cidr_block = "10.0.0.0/16"
  }
}
