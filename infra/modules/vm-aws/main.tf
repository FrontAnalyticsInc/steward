# AWS: instance, data volume, egress-only security group.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "name" { type = string }
variable "instance_size" { type = string }
variable "disk_gb" { type = number }
variable "cloud_init" { type = string }
variable "availability_zone" {
  type    = string
  default = ""
}

# Ubuntu 24.04 LTS, amd64. Canonical's account id is the filter that matters:
# without an owner constraint, `name` alone can match a lookalike AMI published
# by anyone.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_subnet" "chosen" {
  id = tolist(data.aws_subnets.default.ids)[0]
}

# No ingress rules at all. Not "SSH from my IP" — none. Tailscale carries
# everything including SSH, so an inbound rule would only widen the surface in
# front of an operator console that has no authentication of its own.
resource "aws_security_group" "this" {
  name        = "${var.name}-egress-only"
  description = "Egress only; all access is over Tailscale"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description      = "All outbound"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "${var.name}-egress-only" }
}

resource "aws_instance" "this" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_size
  subnet_id              = data.aws_subnet.chosen.id
  vpc_security_group_ids = [aws_security_group.this.id]
  # Needed to reach Tailscale and pull images. Inbound is closed regardless.
  associate_public_ip_address = true
  user_data                   = var.cloud_init

  # Replacing the instance must not replace the data. The volume below is a
  # separate resource for exactly this reason.
  root_block_device {
    # Docker's image store is here, not on the data volume. See the note in
    # modules/vm-gcp/main.tf — the stack pulls about 13GB and 20 fills up
    # mid-pull.
    volume_size = 50
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    # IMDSv2 only. The gateway mounts the Docker socket and runs agent-authored
    # code, so a v1-style unauthenticated metadata endpoint would be one SSRF
    # away from the instance role.
    http_tokens = "required"
  }

  tags = { Name = var.name }
}

resource "aws_ebs_volume" "data" {
  availability_zone = var.availability_zone != "" ? var.availability_zone : aws_instance.this.availability_zone
  size              = var.disk_gb
  type              = "gp3"
  encrypted         = true

  tags = { Name = "${var.name}-data" }

  lifecycle {
    # The wiki, sessions, the approvals queue and the browser profile live here.
    # An accidental `terraform destroy` should take the instance and stop.
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "data" {
  # Nitro ignores this name and presents the volume as /dev/nvmeXn1 anyway,
  # which is why cloud-init discovers the disk rather than trusting a path.
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.this.id
}

output "instance_id" { value = aws_instance.this.id }
output "data_disk_id" { value = aws_ebs_volume.data.id }
