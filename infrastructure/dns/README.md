# DNS (Terraform, Scaleway)

Terraform manages the Scaleway Domains & DNS zone and records for your root domain (e.g. `cat-id.eu`).

## Prerequisites

- Terraform `>= 1.5`
- Scaleway credentials:
  - `SCW_ACCESS_KEY`
  - `SCW_SECRET_KEY`
  - `SCW_DEFAULT_PROJECT_ID` (recommended)

## Configure

```bash
cd infrastructure/dns
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` (gitignored) with your real `records` targets. Tracked `terraform.tfvars.example` is a template only.

**Not managed here:** Spotify, Last.fm, or `APP_BASE_URL`. Those live in [`../compose/env/production.env`](../compose/env/production.env) (gitignored) on the server.

**IPs:** Point **`cat-id.eu`** / **`app.cat-id.eu`** (and any other managed **A** records) at the **LAED base host’s public IPv4** (the flexible IP from **LAED** Terraform `output`, not a second VM in this repo). For apex + app on that same host, both records use that **one** address.

## Apply

```bash
cd infrastructure/dns
terraform init
terraform plan
terraform apply
terraform output nameservers
```

Delegate the domain at your registrar to the printed nameservers if Scaleway should be authoritative.

## Related

- Record JSON for CLI apply: [`dns_records.sample.json`](dns_records.sample.json)
- Full runbook: [`../../docs/deploy_scaleway.md`](../../docs/deploy_scaleway.md)

## If you used the old path

Terraform was previously under `infrastructure/dns-scaleway/terraform/`. If you have local **state** there, copy `terraform.tfstate` (and `.terraform/` if needed) into `infrastructure/dns/`, or run `terraform init` here and use `terraform state` / Scaleway console to avoid duplicate resources.
