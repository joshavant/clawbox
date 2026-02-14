packer {
  required_plugins {
    tart = {
      version = ">= 1.14.0"
      source  = "github.com/cirruslabs/tart"
    }
  }
}

source "tart-cli" "macos-base" {
  // Use Cirrus Labs' pre-built vanilla Sequoia image.
  // Setup Assistant is already complete, SSH is enabled,
  // and the default user is admin/admin.
  vm_base_name = "ghcr.io/cirruslabs/macos-sequoia-vanilla:latest"
  vm_name      = "macos-base"
  cpu_count    = 4
  memory_gb    = 8
  disk_size_gb = 80
  headless     = true

  ssh_username = "admin"
  ssh_password = "admin"
  ssh_timeout  = "120s"
}

build {
  sources = ["source.tart-cli.macos-base"]

  // Grant admin passwordless sudo for Ansible provisioning
  provisioner "shell" {
    inline = [
      "echo 'admin ALL=(ALL) NOPASSWD: ALL' | sudo EDITOR=tee visudo /etc/sudoers.d/admin-nopasswd",
    ]
  }
}
