#!/usr/bin/env bash
# Install Tesseract on a GitHub runner. The runner's Azure apt mirror sometimes fails DNS
# (our first two CI runs), so retry, then fall back to the main Ubuntu archive.
set -u
for attempt in 1 2 3; do
  if sudo apt-get -o Acquire::Retries=3 update -q && \
     sudo apt-get -o Acquire::Retries=3 install -y -q --no-install-recommends tesseract-ocr; then
    tesseract --version | head -1
    exit 0
  fi
  echo "attempt $attempt failed; switching to archive.ubuntu.com"
  sudo sed -i 's|mirror+file:/etc/apt/apt-mirrors.txt|http://archive.ubuntu.com/ubuntu/|g' \
    /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true
  sleep 10
done
# Do not fail the job on a package-mirror outage: only one test needs Tesseract, and it
# skips itself (with a reason in the pytest summary) when the binary is missing.
echo "::warning::tesseract-ocr could not be installed; the real-OCR test will be skipped"
exit 0
