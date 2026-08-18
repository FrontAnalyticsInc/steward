variable "cloud" {
  description = "Which provider to deploy on. The only value that differs between an AWS and a GCP deployment."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "gcp"], var.cloud)
    error_message = "cloud must be \"aws\" or \"gcp\"."
  }
}

variable "name" {
  description = "Name for the instance, disk and firewall rule. Also the Tailscale hostname."
  type        = string
  default     = "hermes"
}

variable "region" {
  description = "AWS region or GCP region."
  type        = string
  default     = "eu-west-1"
}

variable "zone" {
  description = "Availability zone. GCP requires one; AWS derives it from the subnet when left empty."
  type        = string
  default     = ""
}

variable "gcp_project" {
  description = "GCP project id. Ignored when cloud = \"aws\"."
  type        = string
  default     = ""
}

variable "instance_size" {
  description = <<-EOT
    Machine type. Defaults to 8 GB — e2-standard-2 on GCP, t3.large on AWS.

    This used to default to 4 GB and describe it as "the floor". It is not a
    floor. config.yaml allows a single tool sandbox 5 GiB, which on a 4 GB box
    is a limit above physical memory — i.e. not a limit at all — so the first
    tool call that does real work is an OOM candidate, and what an operator sees
    is a flaky agent rather than a machine that is too small. Measured idle is
    ~1.6 GB with no local model; the headroom is for the peaks, not the idle.

    install.sh enforces the same thing: it refuses under 6 GB and warns under 8.

    Stay on amd64. Graviton is roughly 20% cheaper, but the images are built
    linux/amd64 only and an arm64 port is its own piece of work.
  EOT
  type        = string
  default     = ""
}

variable "disk_gb" {
  description = "Size of the separate data disk holding HERMES_DATA_DIR and the approvals queue."
  type        = number
  default     = 40
}

variable "tailscale_authkey" {
  description = <<-EOT
    A Tailscale auth key, ideally ephemeral and pre-authorized. This is the only
    secret Terraform handles, and it is deliberately the only one: it is
    single-purpose, short-lived, and revocable from the Tailscale console
    without touching the box.

    Everything else — the model provider key, Gmail credentials — is copied over
    Tailscale afterwards, because a Terraform variable becomes instance metadata
    and metadata is readable by anything running on the host.
  EOT
  type        = string
  sensitive   = true
}

# repo_url is gone. cloud-init used to clone hermes-infra onto the box, which
# never worked for anyone but its author: the repo is private and cloud-init
# carries no credential, so a fresh VM failed the clone and every step after it.
# The installer needs no checkout — it fetches a checksummed stack file and
# pulls images — so the variable had nothing left to point at.
