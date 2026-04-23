terraform {
  required_version = ">= 1.5.0"

  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = ">= 2.57.0"
    }
  }
}

provider "scaleway" {
  # Auth: SCW_ACCESS_KEY, SCW_SECRET_KEY
  # Optional: SCW_DEFAULT_PROJECT_ID, SCW_DEFAULT_REGION
}
