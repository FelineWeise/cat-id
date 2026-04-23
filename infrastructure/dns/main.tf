locals {
  dns_zone = var.domain
}

resource "scaleway_domain_zone" "this" {
  domain    = var.domain
  subdomain = var.subdomain
  project_id = var.project_id
}

resource "scaleway_domain_record" "managed" {
  for_each = var.records

  dns_zone = local.dns_zone
  name     = each.value.name
  type     = each.value.type
  data     = each.value.data
  ttl      = each.value.ttl
  priority = each.value.priority

  depends_on = [scaleway_domain_zone.this]
}

output "dns_zone" {
  value       = local.dns_zone
  description = "DNS zone name used by Scaleway records."
}

output "nameservers" {
  value       = scaleway_domain_zone.this.ns
  description = "Authoritative nameservers for the created zone. Delegate your domain at the registrar to these values."
}
