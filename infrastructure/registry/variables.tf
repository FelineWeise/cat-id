variable "scw_region" {
  type        = string
  description = "Scaleway region for the registry namespace (e.g. fr-par)."
  default     = "fr-par"
}

variable "namespace_name" {
  type        = string
  description = "Registry namespace name (path segment in rg.<region>.scw.cloud/<name>/...)."
  default     = "cat-id"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.namespace_name))
    error_message = "namespace_name must be 3-32 lowercase alphanumeric characters or hyphens, starting with a letter."
  }
}

variable "is_public" {
  type        = bool
  description = "Whether the namespace is public. Production cat-id images are usually private (false)."
  default     = false
}
