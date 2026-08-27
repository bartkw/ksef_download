# Setup projektu KSeF (Windows / PowerShell) - odpowiednik setup.sh.
# Uruchomienie:  powershell -ExecutionPolicy Bypass -File setup.ps1
# (komunikaty bez polskich znakow - poprawny wydruk takze w Windows PowerShell 5.1)

Set-Location -Path $PSScriptRoot

function Step($m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m"   -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m"    -ForegroundColor Yellow }
function Err($m)  { Write-Host "[X] $m"    -ForegroundColor Red }

$Vendor  = "vendor\ksef-pdf-generator"
$GenRepo = "https://github.com/CIRFMF/ksef-pdf-generator"

# --------------------------------------------------------------------------- #
Step "Python"
$py = $null
foreach ($c in @("python", "py", "python3")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
    Err "Brak Pythona. Zainstaluj Python 3.10+ z https://python.org (zaznacz 'Add to PATH')."
    exit 1
}
$pyv = & $py -c "import sys; print('%d.%d' % sys.version_info[:2])"
Ok "python = $pyv ($py)"

# --------------------------------------------------------------------------- #
Step "Zaleznosci Pythona (requests, cryptography, pypdf)"
& $py -c "import requests, cryptography, pypdf" 2>$null
if ($LASTEXITCODE -eq 0) {
    Ok "juz zainstalowane"
} else {
    Warn "instaluje z requirements.txt..."
    & $py -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { & $py -m pip install --user -r requirements.txt }
    if ($LASTEXITCODE -ne 0) {
        Err "Nie udalo sie zainstalowac zaleznosci Pythona."
        Err "Sprobuj: $py -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt"
        exit 1
    }
    Ok "zainstalowane"
}

# --------------------------------------------------------------------------- #
Step "Node.js (wymagany tylko dla KSEF_FORMAT=pdf)"
$node = $null
if (Get-Command node -ErrorAction SilentlyContinue) {
    $node = (Get-Command node).Source
} elseif (Test-Path "$env:ProgramFiles\nodejs\node.exe") {
    $node = "$env:ProgramFiles\nodejs\node.exe"
}

if (-not $node) {
    Warn "Nie znaleziono Node.js. Tryb PDF nie zadziala bez niego."
    Warn "Zainstaluj Node.js >= 20 z https://nodejs.org (dodaje sie do PATH)."
    Warn "Tryb XML (KSEF_FORMAT=xml) bedzie dzialac bez Node."
} else {
    $nodev = & node -v
    Ok "node = $nodev ($node)"

    Step "Generator PDF (CIRFMF/ksef-pdf-generator)"
    if (-not (Test-Path "$Vendor\package.json")) {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Err "Brak git. Zainstaluj Git dla Windows: https://git-scm.com"
            exit 1
        }
        Warn "klonuje generator do $Vendor ..."
        git clone --depth 1 $GenRepo $Vendor
        Ok "sklonowano"
    } else {
        Ok "generator juz obecny"
    }
    Warn "npm install w $Vendor (swieze, dla tego systemu)..."
    Push-Location $Vendor
    npm install --no-audit --no-fund
    $npmCode = $LASTEXITCODE
    Pop-Location
    if ($npmCode -eq 0) { Ok "zaleznosci generatora zainstalowane" }
    else { Err "npm install nie powiodlo sie (kod $npmCode)." }
}

# --------------------------------------------------------------------------- #
Step "Plik .env"
if (Test-Path ".env") {
    Ok ".env juz istnieje (nie ruszam)"
} else {
    Copy-Item "env.example.txt" ".env"
    Ok "utworzono .env z env.example.txt"
    Warn "Uzupelnij w .env: KSEF_TOKEN oraz KSEF_NIP (i sprawdz KSEF_MODE/KSEF_FORMAT)."
}

Step "Firmy (opcjonalnie, wiele firm)"
if (Test-Path "firmy.json") {
    Ok "firmy.json juz istnieje"
} else {
    Warn "Aby wybierac sposrod wielu firm: 'Copy-Item firmy.example.json firmy.json' i uzupelnij."
    Warn "Bez firmy.json skrypt uzyje pojedynczej firmy z .env."
}

# --------------------------------------------------------------------------- #
Step "Self-check"
$check = @"
import importlib.util
spec = importlib.util.spec_from_file_location('ksef', 'ksef_pobierz.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print('  Python OK, modul laduje sie poprawnie.')
node = m._find_node()
print('  Node wykryty:', node if node else 'NIE (potrzebny do PDF)')
gen = (m.VENDOR_DIR / 'src' / 'index.ts').exists() and (m.VENDOR_DIR / 'node_modules').exists()
print('  Generator PDF gotowy:', 'TAK' if gen else 'NIE (uruchom npm install w vendor)')
"@
& $py -c $check

Write-Host "`nGotowe. Uzupelnij .env i uruchom:  python ksef_pobierz.py" -ForegroundColor Green
