#!/bin/bash
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine . 2>/dev/null
./token-burn install
read -r -p "Press Enter to close"
