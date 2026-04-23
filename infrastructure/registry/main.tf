resource "scaleway_registry_namespace" "cat_id" {
  name       = var.namespace_name
  region     = var.scw_region
  is_public  = var.is_public
}
