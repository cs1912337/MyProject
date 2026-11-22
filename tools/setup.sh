#!/usr/bin/env bash
# Installs the SQLite CLI. Safe to re-run.
set -euo pipefail
if command -v sqlite3 >/dev/null; then sqlite3 --version; exit 0; fi
case "$(uname -s)" in
  Linux)  sudo_cmd=$([ "$(id -u)" -eq 0 ] && echo "" || echo sudo)
          $sudo_cmd apt-get update -qq && $sudo_cmd apt-get install -y -qq sqlite3 ;;
  Darwin) brew install sqlite ;;
  *)      echo "Windows: winget install SQLite.SQLite" >&2; exit 1 ;;
esac
sqlite3 --version
