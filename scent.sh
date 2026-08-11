#!/bin/bash

echo "Scent Ledger Update"
docker tag scent-ledger-scent-ledger ghcr.io/hxck/scent-ledger:latest
docker push ghcr.io/hxck/scent-ledger:latest
echo "Tagged and pushed. Restarting..."
docker compose -f /opt/stacks/frags/compose.yaml pull
docker compose -f /opt/stacks/frags/compose.yaml up -d
echo "Finished."
