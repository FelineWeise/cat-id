# Optional Scaleway Container Registry namespace (`cat-id`)

Minimal Terraform: one **private** `scaleway_registry_namespace` for cat-id images when you do **not** want to share LAED’s registry namespace.

## When to skip this folder

If LAED already provisions a namespace and you are fine pushing cat-id as e.g. `rg.fr-par.scw.cloud/laed/cat-id:<tag>`, you do **not** need to apply this stack—only follow the tagging convention in [../../docs/deploy_scaleway.md](../../docs/deploy_scaleway.md).

## Prerequisites

- Terraform `>= 1.5`
- `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, and preferably `SCW_DEFAULT_PROJECT_ID`

## Apply

```bash
cd infrastructure/registry
cp terraform.tfvars.example terraform.tfvars   # optional overrides
terraform init
terraform plan
terraform apply
terraform output registry_endpoint
```

Use the printed prefix when tagging images, for example:

```bash
docker tag cat-id:latest "$(terraform output -raw registry_endpoint)/app:v1.0.0"
docker push "$(terraform output -raw registry_endpoint)/app:v1.0.0"
```

## Related

- Production runtime: [`../deploy/`](../deploy/) (Compose + Caddy, image-only)
- DNS: [`../dns/`](../dns/)
