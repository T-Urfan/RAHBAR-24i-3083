#!/usr/bin/env bash
# Install Tesseract on a GitHub Actions Ubuntu runner, or stop with a clear diagnostic.
# Never continues with stale or partial package indexes: the real-OCR test must run.
set -euo pipefail

if command -v tesseract >/dev/null 2>&1; then
  echo "tesseract already installed: $(tesseract --version 2>&1 | head -1)"
  exit 0
fi

# Hosts apt really uses: active (non-comment) entries of the sources and of the runner's
# mirror list (azure.archive.ubuntu.com first, archive.ubuntu.com as its fallback).
HOSTS=$(cat /etc/apt/sources.list /etc/apt/sources.list.d/* /etc/apt/apt-mirrors.txt \
          2>/dev/null | grep -v '^\s*#' | grep -oE 'https?://[^/ ]+' \
          | sed -E 's#https?://##' | sort -u || true)
HOSTS=${HOSTS:-archive.ubuntu.com}

# OK when at least one mirror resolves; apt-get update --error-on=any below still fails
# if any index cannot actually be downloaded.
dns_ok() {
  local ok=1
  for host in $HOSTS; do
    if getent hosts "$host" >/dev/null; then echo "  resolves: $host"; ok=0
    else echo "  cannot resolve: $host"; fi
  done
  return $ok
}

for attempt in 1 2 3 4 5; do
  if dns_ok; then
    break
  fi
  if [ "$attempt" -eq 5 ]; then
    echo "::error::no apt host resolves after 5 attempts ($(echo $HOSTS)); the runner has no package-repository access. Re-run the job."
    exit 1
  fi
  echo "DNS attempt $attempt failed; retrying in $((attempt * 10)) s"
  sleep $((attempt * 10))
done

# --error-on=any: any index that fails to download is an error, so apt never installs
# from stale or missing indexes.
for attempt in 1 2 3; do
  if timeout 180 sudo apt-get update -q --error-on=any \
       -o Acquire::Retries=3 -o Acquire::http::Timeout=30; then
    break
  fi
  if [ "$attempt" -eq 3 ]; then
    echo "::error::apt-get update failed 3 times; package indexes are incomplete, not installing."
    exit 1
  fi
  echo "apt-get update attempt $attempt failed; retrying in 15 s"
  sleep 15
done

timeout 300 sudo apt-get install -y -q --no-install-recommends \
  -o Acquire::Retries=3 tesseract-ocr tesseract-ocr-eng
tesseract --version 2>&1 | head -1
