#!/usr/bin/env bash
# Deploy the LinkedIn Profile API.
# Run on the VPS from the checked-out repo. Requires:
#   - API_KEY exported (secret; /profile is gated on it)
#   - cookies.json (full linkedin.com jar) OR LINKEDIN_COOKIES env
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${API_KEY:-}" ]; then
  echo "ERROR: export API_KEY=... before deploying" >&2
  exit 1
fi
if [ ! -f cookies.json ] && [ -z "${LINKEDIN_COOKIES:-}" ]; then
  echo "ERROR: provide cookies.json (full linkedin.com jar) or LINKEDIN_COOKIES" >&2
  exit 1
fi

python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -r requirements.txt

echo "Dependencies installed. Choose a run mode:"
echo "  dev:        uvicorn main:app --host 127.0.0.1 --port 8099"
echo "  systemd:    sudo cp linkedin-api.service /etc/systemd/system/ \\"
echo "              && sudo systemctl daemon-reload && sudo systemctl enable --now linkedin-api"
echo "  docker:     docker build -t linkedin-api . && docker run -p 8099:8099 \\"
echo "              -e API_KEY=\$API_KEY -e LINKEDIN_COOKIES=\$LINKEDIN_COOKIES linkedin-api"
