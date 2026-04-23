output "registry_endpoint" {
  value       = "rg.${var.scw_region}.scw.cloud/${scaleway_registry_namespace.cat_id.name}"
  description = "Image prefix for docker push/pull (e.g. rg.fr-par.scw.cloud/cat-id/app:v1.0.0)."
}

output "namespace_id" {
  value       = scaleway_registry_namespace.cat_id.id
  description = "Scaleway registry namespace resource id (for imports or support)."
}

output "namespace_name" {
  value       = scaleway_registry_namespace.cat_id.name
  description = "Registry namespace name as stored in Scaleway."
}
