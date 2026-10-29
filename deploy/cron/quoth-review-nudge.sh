#!/bin/sh
# TEMPLATE: weekly memory-review nudge for a script-only (no-agent) Hermes cron.
# Prints one line when the ledger has pending claims; empty stdout means nothing
# to review, so nothing is delivered. Read-only. The review itself runs in the
# owner's chat through the memory-review skill.
# Hermes runs cron scripts only from <HERMES_HOME>/scripts/, so this one-line
# wrapper is copied there; the logic lives in `quoth ledger nudge`.
#
# HOST VARIABLES: QUOTH_PREFIX (install-release.sh --prefix) and QUOTH_HOME
# (quoth's state; the ledger is $QUOTH_HOME/ledger.db). Edit the defaults below.
set -eu
QUOTH_PREFIX=${QUOTH_PREFIX:-/srv/quoth/venvs}
QUOTH_HOME=${QUOTH_HOME:-/srv/quoth/state}
export QUOTH_HOME
exec "$QUOTH_PREFIX/quoth-current/bin/quoth" ledger --ledger "$QUOTH_HOME/ledger.db" nudge
