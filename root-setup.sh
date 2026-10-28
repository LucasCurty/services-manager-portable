#!/usr/bin/env bash
# Execute como root: sudo bash /home/servidor-lucas/.local/share/services-manager/root-setup.sh
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Rode com sudo: sudo bash $0" >&2
  exit 1
fi
apt-get install -y gnome-shell-extension-desktop-icons-ng
loginctl enable-linger servidor-lucas
echo
echo "Pronto. Agora, como usuario normal e depois de relogar no xrdp:"
echo "  gnome-extensions enable ding@rastersoft.com"
