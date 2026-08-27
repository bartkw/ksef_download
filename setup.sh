#!/usr/bin/env bash
#
# Setup projektu KSeF (macOS / Linux).
# Sprawdza Pythona i Node, instaluje zależności, przygotowuje generator PDF
# oraz plik .env. Uruchom: bash setup.sh
#
set -euo pipefail

cd "$(dirname "$0")"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
ok()   { printf "%s✓%s %s\n" "$GREEN" "$OFF" "$1"; }
warn() { printf "%s!%s %s\n" "$YELLOW" "$OFF" "$1"; }
err()  { printf "%s✗%s %s\n" "$RED" "$OFF" "$1"; }
step() { printf "\n%s== %s ==%s\n" "$BOLD" "$1" "$OFF"; }

VENDOR="vendor/ksef-pdf-generator"
GENERATOR_REPO="https://github.com/CIRFMF/ksef-pdf-generator"

# --------------------------------------------------------------------------- #
step "Python"
if ! command -v python3 >/dev/null 2>&1; then
  err "Brak python3. Zainstaluj Pythona 3.10+ (macOS: 'brew install python')."
  exit 1
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "python3 = $PYV"

step "Zależności Pythona (requests, cryptography, pypdf)"
if python3 -c 'import requests, cryptography, pypdf' >/dev/null 2>&1; then
  ok "już zainstalowane"
else
  warn "instaluję z requirements.txt..."
  if python3 -m pip install -r requirements.txt 2>/dev/null \
     || python3 -m pip install --user -r requirements.txt 2>/dev/null \
     || python3 -m pip install --break-system-packages -r requirements.txt 2>/dev/null; then
    ok "zainstalowane"
  else
    err "Nie udało się zainstalować zależności Pythona."
    err "Spróbuj w środowisku wirtualnym:  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
  fi
fi

# --------------------------------------------------------------------------- #
step "Node.js (wymagany tylko dla KSEF_FORMAT=pdf)"
NODE_BIN=""
for c in "${KSEF_NODE:-}" node /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node \
         "/mnt/c/Program Files/nodejs/node.exe"; do
  [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 && { NODE_BIN=$(command -v "$c"); break; }
  [ -n "$c" ] && [ -x "$c" ] && { NODE_BIN="$c"; break; }
done

if [ -z "$NODE_BIN" ]; then
  warn "Nie znaleziono Node.js. Tryb PDF nie zadziała bez niego."
  warn "Zainstaluj: macOS 'brew install node'  |  Linux — menedżer pakietów / nvm."
  warn "Tryb XML (KSEF_FORMAT=xml) będzie działać bez Node."
else
  NODEV=$("$NODE_BIN" -v 2>/dev/null || echo "?")
  NODEMAJ=$(printf "%s" "$NODEV" | sed -E 's/^v([0-9]+).*/\1/')
  if [ "${NODEMAJ:-0}" -ge 20 ] 2>/dev/null; then
    ok "node = $NODEV ($NODE_BIN)"
  else
    warn "node = $NODEV — zalecane ≥ 20. Może działać, ale w razie problemów zaktualizuj."
  fi

  step "Generator PDF (CIRFMF/ksef-pdf-generator)"
  if [ ! -f "$VENDOR/package.json" ]; then
    warn "klonuję generator do $VENDOR ..."
    git clone --depth 1 "$GENERATOR_REPO" "$VENDOR"
    ok "sklonowano"
  else
    ok "generator już obecny"
  fi

  warn "npm install w $VENDOR (świeże, dla tego systemu)..."
  ( cd "$VENDOR" && npm install --no-audit --no-fund )
  ok "zależności generatora zainstalowane"
fi

# --------------------------------------------------------------------------- #
step "Plik .env"
if [ -f .env ]; then
  ok ".env już istnieje (nie ruszam)"
else
  cp env.example.txt .env
  ok "utworzono .env z env.example.txt"
  warn "Uzupełnij w .env: KSEF_TOKEN oraz KSEF_NIP (i sprawdź KSEF_MODE/KSEF_FORMAT)."
fi

step "Firmy (opcjonalnie, wiele firm)"
if [ -f firmy.json ]; then
  ok "firmy.json już istnieje"
else
  warn "Aby wybierać spośród wielu firm: 'cp firmy.example.json firmy.json' i uzupełnij."
  warn "Bez firmy.json skrypt użyje pojedynczej firmy z .env."
fi

# --------------------------------------------------------------------------- #
step "Self-check"
python3 - <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("ksef", "ksef_pobierz.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print("  Python OK, moduł ładuje się poprawnie.")
node = m._find_node()
print("  Node wykryty:", node if node else "NIE (potrzebny do PDF)")
gen_ok = (m.VENDOR_DIR / "src" / "index.ts").exists() and (m.VENDOR_DIR / "node_modules").exists()
print("  Generator PDF gotowy:", "TAK" if gen_ok else "NIE (uruchom npm install w vendor/ksef-pdf-generator)")
PY

printf "\n%sGotowe.%s Uzupełnij .env i uruchom:  %spython3 ksef_pobierz.py%s\n" "$GREEN$BOLD" "$OFF" "$BOLD" "$OFF"
