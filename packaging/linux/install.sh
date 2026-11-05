#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1
./token-burn install
read -r -p "Press Enter to close"
