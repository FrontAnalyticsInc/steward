#!/usr/bin/env bash
#
# Steward installer — PLACEHOLDER.
#
# This script does not install anything. It exists so the address on the
# landing page resolves to something real and harmless while the actual
# bootstrap is being finished.
#
# It deliberately takes no action: no packages, no containers, no files, no
# network calls beyond fetching itself. People are being invited to pipe this
# into a shell, and a placeholder that half-configures a stranger's server
# would be far worse than one that prints a paragraph and stops.
#
set -euo pipefail

PAGE="https://frontanalytics.com/steward/"
REPO="https://github.com/FrontAnalyticsInc/steward"

bold=""; dim=""; reset=""
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  bold="$(tput bold)"; dim="$(tput dim)"; reset="$(tput sgr0)"
fi

cat <<EOF

  ${bold}Steward${reset} — the business automation factory

  ${bold}Nothing was installed.${reset} The public installer is not finished yet, and
  this script is a placeholder standing at its address.

  Steward runs today. What does not exist yet is a single command that can
  set it up on someone else's machine without supervision, so publishing one
  would be a promise the software cannot keep.

  ${dim}What you can do now${reset}

    Read what it does      ${PAGE}
    Watch the repository   ${REPO}
    Ask for a walkthrough  ${PAGE%steward/}contact/

  You will need a Linux server of its own, an Anthropic or OpenAI key, and a
  machine that stays awake. Nothing on this box was changed by running this.

EOF

exit 0
