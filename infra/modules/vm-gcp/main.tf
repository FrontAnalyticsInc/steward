# GCP: instance, data disk, egress-only firewall.
#
# Same variable names and the same outputs as modules/vm-aws, so the root module
# treats them interchangeably.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "name" { type = string }
variable "instance_size" { type = string }
variable "disk_gb" { type = number }
variable "cloud_init" { type = string }
variable "project" { type = string }
variable "region" { type = string }
variable "zone" { type = string }

# Ubuntu 24.04 LTS, amd64 — the same image family as the AWS module, which is
# what lets one cloud-init serve both. Do NOT swap this for Container-Optimized
# OS: it ships a restricted cloud-init that will not run the write_files and
# runcmd blocks this deployment depends on.
data "google_compute_image" "ubuntu" {
  family  = "ubuntu-2404-lts-amd64"
  project = "ubuntu-os-cloud"
}

# GCP allows all egress by default and denies ingress by default, so the only
# rule worth writing is the explicit deny that says so. Access is over
# Tailscale; there is no SSH rule on purpose.
resource "google_compute_firewall" "deny_ingress" {
  name    = "${var.name}-deny-ingress"
  network = "default"

  direction = "INGRESS"
  priority  = 1000

  deny {
    protocol = "all"
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.name]
}

resource "google_compute_disk" "data" {
  name    = "${var.name}-data"
  type    = "pd-balanced"
  zone    = var.zone
  size    = var.disk_gb
  project = var.project

  lifecycle {
    # Holds the wiki, sessions, the approvals queue and the browser profile.
    prevent_destroy = true
  }
}

resource "google_compute_instance" "this" {
  name         = var.name
  machine_type = var.instance_size
  zone         = var.zone
  project      = var.project
  tags         = [var.name]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      # 50, not 20. Docker's image store lives on the BOOT disk — the data disk
      # holds state, not images — and the stack pulls roughly 13GB: gateway 4.0,
      # browser 3.7, workflows 2.3 shared with review-executor, then the console
      # and docs, plus the 2.2GB tool-sandbox base fetched on the first tool
      # call. Ubuntu and cloud-init's 4GB swapfile are on here too. At 20GB the
      # pull fills the disk partway through and the failure reads as a network
      # problem.
      size = 50
      type = "pd-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "hermes-data"
  }

  network_interface {
    network = "default"
    # An ephemeral external address, for egress only — Tailscale and image
    # pulls need it, and the firewall above denies every inbound packet.
    access_config {}
  }

  metadata = {
    # The key GCE's cloud-init reads. Same file AWS passes as user_data.
    user-data = var.cloud_init

    # The v0.1/v1beta1 metadata endpoints answer without the
    # Metadata-Flavor header, so anything that can make the instance issue an
    # HTTP GET can read metadata — including the service-account token. This
    # box runs agent-authored code with a Docker socket, so that is not
    # theoretical. The v1 endpoint, which requires the header, is unaffected.
    disable-legacy-endpoints = "true"

    # Project-wide SSH keys do not apply here. Access is over Tailscale SSH;
    # a key added to the project should not silently grant a shell on the one
    # host in it that reads mail.
    block-project-ssh-keys = "true"
  }

  # No service account. The default compute SA is attached automatically
  # otherwise, and on many projects it carries broad roles — which an agent
  # running arbitrary code in a sandbox could use against the rest of the
  # project. Nothing in this stack calls a Google API with instance
  # credentials: Gmail and Calendar use their own service-account files,
  # delegated and scoped separately.
  service_account {
    email  = null
    scopes = []
  }
}

output "instance_id" { value = google_compute_instance.this.instance_id }
output "data_disk_id" { value = google_compute_disk.data.id }
