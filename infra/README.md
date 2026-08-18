# Infrastructure

One VM, one data disk, one egress-only firewall — on AWS or GCP, from the same
configuration.

```
infra/
  cloud-init.yaml          100% shared; does all the real configuration
  main.tf                  picks a module, passes identical variables
  variables.tf
  modules/vm-aws/          instance, EBS volume, security group
  modules/vm-gcp/          instance, PD, firewall rule
  check_cloud_init.py      renders and parses cloud-init before you apply
```

## The portability is cloud-init, not Terraform

Both EC2 (`user_data`) and GCE (`user-data` metadata) run cloud-init natively on
Ubuntu cloud images. One YAML installs Docker, discovers and mounts the data
disk, joins Tailscale and clones the repo — so the provider-specific Terraform
is two ~100-line modules with the same variable interface and the same outputs.

Adding a third provider is another module against the same cloud-init. Hetzner
has a Terraform provider and cloud-init-enabled images, and is roughly a third
of the price; if AWS/GCP is not a hard requirement, that is the cheaper answer.

## Deploy

```bash
# GCP: authenticate Terraform and enable the API once per project.
gcloud auth application-default login
gcloud services enable compute.googleapis.com --project=<your-project>

cd infra
cp gcp.tfvars.example gcp.tfvars     # fill in project and Tailscale key
terraform init
terraform apply -var-file=gcp.tfvars
```

AWS is the same shape:

```bash
terraform apply \
  -var cloud=aws \
  -var tailscale_authkey=tskey-auth-...
```

Then, from a machine on the same tailnet:

```bash
ssh steward
curl -fsSL https://raw.githubusercontent.com/FrontAnalyticsInc/steward/main/install.sh | bash
```

The installer asks for an Anthropic API key and a GitHub token with
`read:packages`, writes `/srv/steward/stack/.env`, pulls the images and starts
the stack. cloud-init has already run `tailscale serve`, so the console is at
`https://steward.<your-tailnet>.ts.net` when it finishes.

Terraform does not start the stack, and cloud-init does not clone anything.
Both are deliberate — see Secrets below.

## Choices worth knowing

**Ubuntu 24.04 LTS, amd64, on both.** This is the anchor that makes one
cloud-init work everywhere. Do **not** switch to Container-Optimized OS (GCP) or
Bottlerocket (AWS): both ship a restricted cloud-init that will not run the
`write_files` and `runcmd` blocks here. Stay on amd64 — Graviton is ~20%
cheaper, but six images build from source and an arm64 port is its own project.

**Zero inbound firewall rules.** Not "SSH from my IP" — none. Tailscale carries
everything including SSH (`tailscale up --ssh`), so an inbound rule would only
widen the surface in front of an operator console that has no authentication of
its own. If you cannot reach the box over Tailscale, it is unreachable; that is
the intended failure mode.

**Images are built in CI, not on the box.** `.github/workflows/build-images.yml`
pushes to GHCR, which is the registry that works unchanged on both providers
(ECR and Artifact Registry do not). The `deploy` and `standalone` overlays are
rendered together into one `steward-stack.yml`, published as a release artifact
and checksummed; the box needs no checkout at all. `infra/check_standalone.py`
runs in CI and fails the build if that rendered file would need one.

**The data disk is a separate resource with `prevent_destroy`.** It holds the
wiki, sessions, the approvals queue and the browser profile. An accidental
`terraform destroy` should take the instance and stop.

## Secrets

Terraform handles exactly one: the Tailscale auth key. It is single-purpose,
short-lived and revocable from the Tailscale console without touching the box.

Everything else — the Anthropic key, the registry token — is typed into the
installer over Tailscale after the box is up. **A Terraform variable becomes
instance metadata, and instance metadata is readable by anything running on the
host**, including code the agent writes and executes in a tool sandbox. One
interactive prompt is a better trade than putting a billable credential
somewhere the agent can read it.

The per-install secrets — `API_SERVER_KEY`, the dashboard password and its
session secret — are generated on the box by install.sh and never leave it.
Nothing is copied between deployments; two boxes sharing them would each be able
to act as the other.

If the prompt becomes tedious, the portable upgrade is a SOPS-encrypted `.env`
with the age key delivered at boot — not moving secrets into Terraform.

## Three places the abstraction leaks

Handled explicitly rather than pretended away:

1. **Block device naming.** AWS Nitro presents the volume as `/dev/nvme1n1`
   regardless of the `device_name` you ask for; GCP uses
   `/dev/disk/by-id/google-*`. `cloud-init.yaml` discovers the first
   unpartitioned non-root disk instead of trusting a path, formats it only if it
   has no filesystem, and mounts by UUID.

2. **Secrets.** No common interface. See above.

3. **Terraform state.** S3 and GCS backends differ. For a single VM, local state
   in a backed-up location or Terraform Cloud is fine; do not build a
   state-backend abstraction for three resources.

## Verify

```bash
python3 infra/check_cloud_init.py     # renders and parses before you apply
terraform validate
```

The test worth running once, while nothing depends on the box:

```bash
# GCP
terraform taint 'module.vm_gcp[0].google_compute_instance.this'
terraform apply -var-file=gcp.tfvars

# AWS
terraform taint 'module.vm_aws[0].aws_instance.this'
```

The data disk must reattach with the wiki, sessions, approvals and browser
profile intact. That validates the IaC and doubles as the disaster-recovery
drill. Doing it on **both** providers once is what proves the abstraction — do
it on one and you have portability on paper.

## Cost

Approximate list price at 730 hours. Check current rates rather than trusting
these — they are here for the shape of the comparison, not the precision.

| | 6 GB | 8 GB (default) | 16 GB |
|---|---|---|---|
| GCP | `e2-custom-2-6144` ~$37 | `e2-standard-2` ~$49 | `e2-standard-4` ~$98 |
| AWS | — | `t3.large` ~$61 | `t3.xlarge` ~$121 |

**GCP is the cheaper of the two at both sizes**, by roughly 20%. If you are
already on GCP there is no cost argument for moving; AWS would cost more and a
migration on top.

Two things that are easy to get wrong:

- **E2 does not receive sustained-use discounts.** The family is priced lower up
  front instead. What reduces the bill is a committed-use discount, which is a
  1- or 3-year commitment.
- **Custom machine shapes are a GCP-only lever.** `e2-custom-2-6144` buys 6 GB
  rather than forcing a jump to 8 — about $37/month, and the least you can
  spend that still works.

**Start at `e2-standard-2`.** An earlier version of this file recommended
`e2-medium` and called 4 GB "both the floor and the value". That was wrong, and
wrong in the way that is hardest to diagnose: `config.yaml` allows a single tool
sandbox 5 GiB, so on a 4 GB box the limit sits above physical memory and is not
a limit at all. The first tool call doing real work gets OOM-killed, and what an
operator sees is an agent that is unreliable rather than a machine that is too
small. `install.sh` refuses under 6 GB for the same reason.

Idle is only ~1.6 GB with no local model. The headroom is not for the idle — it
is for the peaks, and the peaks are what the sandbox limit describes.

cloud-init still provisions a 4 GB swap file, which keeps a sandbox and a
browser render peaking together from becoming an OOM kill. Swap absorbs a spike;
it does not substitute for memory a sandbox is entitled to claim. Watch `docker
stats` and `free -h` under real use for a week; moving up is a machine-type
change and a reboot, and the data disk is separate so nothing is lost.

Note the boot disk is 50 GB, not the 20 it was: Docker's image store lives
there, and the stack pulls about 13 GB.
