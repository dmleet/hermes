#!/bin/sh
# Prepare the local directories the compose stack bind-mounts. Safe to re-run.
set -eu
cd "$(dirname "$0")/.."

mkdir -p dev/data dev/music dev/beets \
         dev/downloads/pending dev/downloads/complete dev/downloads/torrents \
         dev/deluge/config dev/prowlarr/config

# Deluge merges a partial core.conf with its defaults on first start: this seeds the
# pending -> complete layout and the Label plugin, matching production.
if [ ! -f dev/deluge/config/core.conf ]; then
  cp dev/deluge/core.conf.template dev/deluge/config/core.conf
  echo "seeded dev/deluge/config/core.conf"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from .env.example"
fi
if [ ! -f dev/hermes/config.yaml ]; then
  cp dev/hermes/config.example.yaml dev/hermes/config.yaml
  echo "created dev/hermes/config.yaml from its example (gitignored; add your ListenBrainz name there)"
fi

# The Prowlarr API key is generated per checkout (never committed) and shared between the
# seeded config.xml, .env (host CLI + scripts) and the hermes container (via compose).
key=$(grep -E '^PROWLARR_API_KEY=[0-9a-f]{32}$' .env | cut -d= -f2 || true)
if [ -z "$key" ]; then
  key=$(python -c 'import secrets; print(secrets.token_hex(16))')
  if grep -q '^PROWLARR_API_KEY=' .env; then
    sed -i "s/^PROWLARR_API_KEY=.*/PROWLARR_API_KEY=$key/" .env
  else
    printf 'PROWLARR_API_KEY=%s\n' "$key" >> .env
  fi
  echo "generated PROWLARR_API_KEY in .env"
fi

# Prowlarr reads config.xml at start: the generated key and external auth mean no
# first-run wizard. The PandaCD indexer is added by scripts/dev-prowlarr-setup.py.
if [ ! -f dev/prowlarr/config/config.xml ]; then
  sed "s/__PROWLARR_API_KEY__/$key/" dev/prowlarr/config.xml.template > dev/prowlarr/config/config.xml
  echo "seeded dev/prowlarr/config/config.xml"
fi

echo "ready: docker compose up --build -d"
echo "then:  uv run python scripts/dev-prowlarr-setup.py"
echo "       docker compose exec beets beets-python /scripts/seed-dev-library.py"
