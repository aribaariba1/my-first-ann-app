#!/usr/bin/env bash
# ==============================================================================
# Build standalone PyInstaller binary archive for the Smart Kitchen Assistant
# Python Executa (`my-first-anna-app`).
#
# Output in `executas/my-first-anna-app/dist/`:
#   Unix:    my-first-anna-app-<version>-<platform>.tar.gz  (+ .sha256)
#            containing: tool-dev-my-first-anna-app, manifest.json
#   Windows: my-first-anna-app-<version>-<platform>.zip     (+ .sha256)
#            containing: tool-dev-my-first-anna-app.exe, manifest.json
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

EXECUTA_SLUG="my-first-anna-app"
ENTRYPOINT_STEM="tool-dev-my-first-anna-app"
PLUGIN_FILE="my_first_anna_app_plugin.py"

VERSION="${VERSION:-$(python -c 'import json; print(json.load(open("executa.json", encoding="utf-8"))["version"])')}"

# Detect canonical Anna platform key (`<os>-<arch>`) unless overridden by $PLATFORM
if [[ -z "${PLATFORM:-}" ]]; then
  UNAME_S="$(uname -s | tr '[:upper:]' '[:lower:]')"
  UNAME_M="$(uname -m | tr '[:upper:]' '[:lower:]')"
  case "$UNAME_M" in
    x86_64|amd64|x64) ARCH="x86_64" ;;
    arm64|aarch64)    ARCH="arm64" ;;
    *)                ARCH="$UNAME_M" ;;
  esac
  case "$UNAME_S" in
    darwin*)                   OS="darwin" ;;
    linux*)                    OS="linux" ;;
    msys*|mingw*|cygwin*|nt*)  OS="windows" ;;
    *)                         OS="$UNAME_S" ;;
  esac
  PLATFORM="${OS}-${ARCH}"
fi

if [[ "$PLATFORM" == windows-* ]]; then
  ENTRYPOINT_BIN="${ENTRYPOINT_STEM}.exe"
  ARCHIVE_EXT="zip"
else
  ENTRYPOINT_BIN="${ENTRYPOINT_STEM}"
  ARCHIVE_EXT="tar.gz"
fi

ARCHIVE_NAME="${EXECUTA_SLUG}-${VERSION}-${PLATFORM}.${ARCHIVE_EXT}"

echo "==> Building ${EXECUTA_SLUG} v${VERSION} for ${PLATFORM}"
echo "    Entrypoint inside archive: ${ENTRYPOINT_BIN}"
echo "    Output archive:            dist/${ARCHIVE_NAME}"

rm -rf build dist "${ENTRYPOINT_STEM}.spec"

# 1. Build single-file executable with PyInstaller
pyinstaller \
  --onefile \
  --clean \
  --noupx \
  --name "${ENTRYPOINT_STEM}" \
  "${PLUGIN_FILE}"

test -f "dist/${ENTRYPOINT_BIN}"

# 2. Smoke-test the built binary via stdio JSON-RPC `describe`
echo "==> Smoke-testing dist/${ENTRYPOINT_BIN}"
python - "dist/${ENTRYPOINT_BIN}" "${ENTRYPOINT_STEM}" <<'PY'
import json, subprocess, sys

binary, expected_name = sys.argv[1], sys.argv[2]
proc = subprocess.Popen(
    [binary],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
    encoding="utf-8",
)
try:
    proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"describe"}\n')
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, "No response from binary on `describe`"
    resp = json.loads(line)
    result = resp.get("result") or {}
    assert result.get("name") == expected_name, f"Unexpected manifest name: {result.get('name')}"
    assert "llm.sample" in (result.get("host_capabilities") or []), "Missing llm.sample capability"
    print(f"    describe OK: name={result.get('name')} version={result.get('version')}")
finally:
    proc.stdin.close()
    proc.wait(timeout=5)
PY

# 3. Copy in-archive manifest.json and package archive + SHA-256 checksum
cp manifest.json dist/manifest.json

(
  cd dist
  if [[ "$ARCHIVE_EXT" == "zip" ]]; then
    python - "${ARCHIVE_NAME}" "${ENTRYPOINT_BIN}" "manifest.json" <<'PY'
import sys, zipfile
archive_name, *members = sys.argv[1:]
with zipfile.ZipFile(archive_name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for m in members:
        zf.write(m, arcname=m)
PY
  else
    chmod 0755 "${ENTRYPOINT_BIN}"
    tar czf "${ARCHIVE_NAME}" "${ENTRYPOINT_BIN}" manifest.json
  fi

  # Generate standard `<sha256>  <filename>` checksum file
  python - "${ARCHIVE_NAME}" <<'PY'
import hashlib, pathlib, sys
target = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(target.read_bytes()).hexdigest()
checksum_line = f"{digest}  {target.name}\n"
pathlib.Path(f"{target.name}.sha256").write_text(checksum_line, encoding="utf-8")
print(f"==> SHA-256: {checksum_line.strip()}")
PY
)

echo "==> Done: dist/${ARCHIVE_NAME} and dist/${ARCHIVE_NAME}.sha256"

