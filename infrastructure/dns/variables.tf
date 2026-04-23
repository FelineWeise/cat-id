variable "domain" {
  type        = string
  description = "Root domain for the DNS zone (example: cat-id.eu)."
}

variable "subdomain" {
  type        = string
  description = "Subdomain segment for the Scaleway DNS zone. Use empty string for apex zone (example: cat-id.eu zone uses subdomain=\"\")."
  default     = ""
}

variable "project_id" {
  type        = string
  description = "Scaleway project ID. Prefer SCW_DEFAULT_PROJECT_ID env var; set this only if you want Terraform to override it explicitly."
  default     = null
}

variable "records" {
  type = map(object({
    name     = string
    type     = string
    data     = string
    ttl      = optional(number, 300)
    priority = optional(number, 0)
  }))
  description = "DNS records to manage declaratively. Keys are stable Terraform resource identifiers (not DNS names)."
}
