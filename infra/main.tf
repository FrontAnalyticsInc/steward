# ==============================================================================
# One VM, one data disk, one egress-only firewall — on either provider.
# ==============================================================================
#
# The stack is Docker Compose on a single host, so the cloud-specific surface is
# three resources. That is what makes real portability achievable here without a
# leaky abstraction: the modules below have an identical variable interface and
# both feed the same cloud-init file, which does all the actual configuration.
#
# Pick with `-var cloud=gcp`. Only one module is ever instantiated; the other's
# count is zero, so neither provider's credentials are needed to plan the other.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}

provider "google" {
  project = var.gcp_project
  region  = var.region
}

locals {
  # Rendered once and handed to whichever module runs. Everything provider-
  # specific has already been resolved by this point.
  cloud_init = templatefile("${path.module}/cloud-init.yaml", {
    tailscale_authkey = var.tailscale_authkey
    hostname          = var.name
  })

  # 8 GB on both, unless overridden. See variables.tf for why this is not 4.
  instance_size = var.instance_size != "" ? var.instance_size : (
    var.cloud == "aws" ? "t3.large" : "e2-standard-2"
  )
}

module "vm_aws" {
  count  = var.cloud == "aws" ? 1 : 0
  source = "./modules/vm-aws"

  name              = var.name
  instance_size     = local.instance_size
  disk_gb           = var.disk_gb
  cloud_init        = local.cloud_init
  availability_zone = var.zone
}

module "vm_gcp" {
  count  = var.cloud == "gcp" ? 1 : 0
  source = "./modules/vm-gcp"

  name          = var.name
  instance_size = local.instance_size
  disk_gb       = var.disk_gb
  cloud_init    = local.cloud_init
  project       = var.gcp_project
  region        = var.region
  zone          = var.zone != "" ? var.zone : "${var.region}-b"
}

output "instance_id" {
  description = "Provider-assigned id for the instance."
  value       = var.cloud == "aws" ? one(module.vm_aws[*].instance_id) : one(module.vm_gcp[*].instance_id)
}

output "data_disk_id" {
  description = "The disk that must survive an instance replacement."
  value       = var.cloud == "aws" ? one(module.vm_aws[*].data_disk_id) : one(module.vm_gcp[*].data_disk_id)
}

output "next_steps" {
  description = "There is no public address on purpose — reach it over Tailscale."
  value       = <<-EOT
    The host has no inbound firewall rules and no published ports. Reach it over
    Tailscale as "${var.name}", then run the installer:

        ssh ${var.name}
        curl -fsSL https://raw.githubusercontent.com/FrontAnalyticsInc/steward/main/install.sh | bash

    It asks for an Anthropic API key and a GitHub token with read:packages.
    Neither is passed through Terraform on purpose — variables become instance
    metadata, and metadata is readable by anything on the box, agent tool calls
    included. cloud-init has already run `tailscale serve` for the console, so
    when the installer finishes it is at:

        https://${var.name}.<your-tailnet>.ts.net

    Disaster-recovery check, worth doing once while nothing depends on it:
    `terraform taint` the instance, re-apply, and confirm the data disk
    reattaches with the wiki, sessions, approvals and browser profile intact.
  EOT
}
