#!/usr/bin/env bash
# gerado automaticamente para o servico 9router; edite ~/.config/services/9router.conf
set -euo pipefail
cd -- /home/servidor-lucas
exec /home/servidor-lucas/.nvm/versions/node/v24.21.0/bin/9router --host 127.0.0.1 --port 20128 --no-browser --skip-update --log
