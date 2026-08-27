#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pobieranie faktur z KSeF 2.0 przy użyciu tokena KSeF.

Flow (zgodnie z dokumentacją MF: github.com/CIRFMF/ksef-docs):
  1. POST /auth/challenge                 -> challenge + timestamp
  2. szyfrowanie "token|timestamp" kluczem publicznym MF (RSA-OAEP SHA-256)
  3. POST /auth/ksef-token                -> referenceNumber + tymczasowy token
  4. GET  /auth/{referenceNumber}         -> oczekiwanie na status = sukces
  5. POST /auth/token/redeem             -> accessToken (Bearer)
  6. POST /invoices/query/metadata        -> lista ksefNumber (osobno sprzedaż/zakup)
  7. GET  /invoices/ksef/{ksefNumber}     -> XML faktury

UWAGA: KSeF 2.0 to świeże API. Dokładny prefix ścieżki (np. /api/v2) oraz
wielkość liter w polach JSON warto potwierdzić ze Swaggerem danego środowiska:
  prod: https://api.ksef.mf.gov.pl/docs/v2
  demo: https://api-demo.ksef.mf.gov.pl/docs/v2
  test: https://api-test.ksef.mf.gov.pl/docs/v2
Wszystkie miejsca do ewentualnej korekty są zebrane w sekcji KONFIGURACJA.
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.x509 import load_der_x509_certificate

# --------------------------------------------------------------------------- #
# KONFIGURACJA
# --------------------------------------------------------------------------- #

# Adresy bazowe API dla poszczególnych środowisk.
# Jeśli okaże się, że endpointy są pod innym prefiksem (np. bez /api/v2),
# popraw tutaj — reszta kodu buduje URL-e względem tej bazy.
BASE_URLS = {
    "prod": "https://api.ksef.mf.gov.pl/api/v2",
    "demo": "https://api-demo.ksef.mf.gov.pl/api/v2",
    "test": "https://api-test.ksef.mf.gov.pl/api/v2",
}

# Typy podmiotu w zapytaniu o metadane:
#   subject1 = sprzedawca/wystawca  -> faktury SPRZEDAŻOWE (wychodzące)
#   subject2 = nabywca              -> faktury ZAKUPOWE (przychodzące)
SUBJECT_SPRZEDAZ = "subject1"
SUBJECT_ZAKUP = "subject2"

# Dopuszczalne typy daty filtrowania (enum KSeF InvoiceQueryDateType).
# Klucz = wartość znormalizowana (lowercase), wartość = kanoniczna nazwa dla API.
DATE_TYPES = {
    "issue": "Issue",                      # data wystawienia faktury
    "invoicing": "Invoicing",              # data przyjęcia w KSeF
    "permanentstorage": "PermanentStorage",  # data trwałego zapisu w repozytorium
}
DEFAULT_DATE_TYPE = "Issue"

# Nazwa pola z datą w metadanych faktury dla danego typu daty — wg niej faktury
# są przydzielane do folderów miesięcznych (YYYY-MM).
DATE_FIELD = {
    "Issue": "issueDate",
    "Invoicing": "invoicingDate",
    "PermanentStorage": "permanentStorageDate",
}
UNKNOWN_MONTH = "nieznana-data"

# Tryb pracy: "single" = pobieranie faktura po fakturze (metadane + GET),
#             "export" = asynchroniczny eksport zaszyfrowanej paczki ZIP.
MODES_SINGLE = {"single", "pojedynczo"}
MODES_EXPORT = {"export", "eksport"}

# Format zapisu faktur na dysku: "xml" (oryginał) lub "pdf" (wizualizacja MF).
FORMATS = {"xml", "pdf"}
DEFAULT_FORMAT = "xml"

# Oficjalny generator PDF MF (CIRFMF/ksef-pdf-generator) wołany dla trybu pdf.
VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "ksef-pdf-generator"
PDF_CLI = "convert-cli.cjs"

# Źródło wsadowego konwertera (wstrzykiwane do katalogu generatora, bo musi mieć
# dostęp do jego node_modules i src/). Wejście/wyjście przez JSON+base64.
PDF_CLI_SOURCE = r'''// Auto-generowany przez ksef_pobierz.py — nie edytuj ręcznie.
const path = require('path');
const fs = require('fs');
const { JSDOM } = require('jsdom');

// Wycisz gadatliwy debug i18next/pdfmake (idzie przez console.*), zanim
// zaimportujemy bibliotekę. Błędy krytyczne piszemy zachowaną referencją.
const _err = console.error.bind(console);
const noop = () => {};
console.log = noop; console.info = noop; console.warn = noop; console.debug = noop; console.error = noop;

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', { url: 'http://localhost/' });
const w = dom.window;
for (const k of ['window', 'document', 'navigator', 'File', 'Blob', 'FileReader',
                 'HTMLElement', 'Node', 'DOMParser', 'XMLSerializer', 'getComputedStyle']) {
  if (w[k] !== undefined) global[k] = w[k];
}
global.self = w;

const jiti = require('jiti')(__filename, {
  alias: { '@shared': path.resolve(__dirname, 'src/shared') },
  interopDefault: true,
});
const { generateInvoice } = jiti('./src/index.ts');

(async () => {
  const jobPath = process.argv[2];
  const job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
  const results = [];
  for (const it of job.items) {
    try {
      const bytes = Buffer.from(it.xmlB64, 'base64');
      const file = new global.File([bytes], 'faktura.xml', { type: 'application/xml' });
      const pdfB64 = await generateInvoice(
        file,
        { nrKSeF: it.nrKSeF, acDate: it.acDate, qrCode: it.qrCode },
        'base64',
      );
      results.push({ id: it.id, pdfB64 });
    } catch (e) {
      results.push({ id: it.id, error: String((e && e.message) || e) });
    }
  }
  fs.writeFileSync(jobPath + '.result', JSON.stringify({ results }));
  process.stdout.write('DONE ' + results.length + '\n');
})().catch((e) => { _err('FATAL', (e && e.stack) || e); process.exit(1); });
'''

# Host weryfikacji QR zależnie od środowiska.
QR_HOSTS = {
    "prod": "https://qr.ksef.mf.gov.pl",
    "demo": "https://qr-demo.ksef.mf.gov.pl",
    "test": "https://qr-test.ksef.mf.gov.pl",
}

PAGE_SIZE = 100
POLL_INTERVAL_S = 2          # co ile sekund odpytywać status uwierzytelnienia
POLL_TIMEOUT_S = 120         # maks. czas oczekiwania na uwierzytelnienie
EXPORT_POLL_TIMEOUT_S = 600  # eksport paczki bywa dłuższy niż uwierzytelnienie
HTTP_TIMEOUT_S = 60

# Katalog bazowy na faktury; wewnątrz tworzone są podfoldery per firma.
FAKTURY_DIR = Path(__file__).resolve().parent / "faktury"


# --------------------------------------------------------------------------- #
# NARZĘDZIA
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path) -> None:
    """Prościutki loader .env (bez zależności zewnętrznych)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"[BŁĄD] Brak zmiennej środowiskowej {name}. Uzupełnij plik .env "
                 f"(wzór: env.example.txt).")
    return val


def log(msg: str) -> None:
    print(f"[KSeF] {msg}", flush=True)


def _add_months(d: date, n: int) -> date:
    """Dodaje n miesięcy do daty, przycinając dzień do ostatniego dnia miesiąca."""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def date_windows(date_from: str, date_to: str, months: int = 3):
    """Dzieli zakres [date_from, date_to] na okna o długości < `months` miesięcy.

    KSeF ogranicza pojedyncze zapytanie o metadane do maks. 3 miesięcy
    (kod błędu 21405), więc szersze zakresy trzeba pobierać w kawałkach.
    """
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    cur = start
    while cur <= end:
        win_end = min(_add_months(cur, months) - timedelta(days=1), end)
        yield cur.isoformat(), win_end.isoformat()
        cur = win_end + timedelta(days=1)


def normalize_date_type(value: str) -> str:
    """Mapuje wartość KSEF_DATE_TYPE na kanoniczną nazwę enuma KSeF."""
    key = (value or "").strip().lower()
    if key not in DATE_TYPES:
        sys.exit(f"[BŁĄD] Nieznany KSEF_DATE_TYPE={value!r}. "
                 f"Dozwolone: Issue, Invoicing, PermanentStorage.")
    return DATE_TYPES[key]


def month_from_meta(meta: dict, date_type: str) -> str:
    """Zwraca folder miesięczny 'YYYY-MM' na podstawie metadanych faktury."""
    field = DATE_FIELD.get(date_type, "issueDate")
    val = meta.get(field) or meta.get("issueDate") or ""
    # Daty to 'YYYY-MM-DD' lub ISO 'YYYY-MM-DDTHH:...'; pierwsze 7 znaków = 'YYYY-MM'.
    return str(val)[:7] if len(str(val)) >= 7 else UNKNOWN_MONTH


def month_from_ksef(ksef_number: str) -> str:
    """Zapasowo wyznacza 'YYYY-MM' z numeru KSeF (format NIP-YYYYMMDD-...)."""
    parts = str(ksef_number).split("-")
    if len(parts) >= 2 and len(parts[1]) >= 6 and parts[1][:6].isdigit():
        d = parts[1]
        return f"{d[:4]}-{d[4:6]}"
    return UNKNOWN_MONTH


def remove_sibling_xml(pdf_dest: Path) -> None:
    """Usuwa bliźniaczy plik .xml obok wygenerowanego PDF (tryb pdf nie trzyma XML)."""
    xml = pdf_dest.with_suffix(".xml")
    if xml.exists():
        try:
            xml.unlink()
            log(f"    usunięto XML {xml.parent.name}/{xml.name}")
        except OSError as e:
            log(f"    nie udało się usunąć {xml.name}: {e}")


# --------------------------------------------------------------------------- #
# ZESTAWIENIA / PODSUMOWANIA
# --------------------------------------------------------------------------- #

# Kolumny rejestru CSV: (klucz w rekordzie, nagłówek w pliku).
CSV_COLUMNS = [
    ("typ", "Typ"),
    ("ksef", "Numer KSeF"),
    ("numer", "Numer faktury"),
    ("data_wystawienia", "Data wystawienia"),
    ("sprzedawca_nip", "NIP sprzedawcy"),
    ("sprzedawca_nazwa", "Sprzedawca"),
    ("nabywca_nip", "NIP nabywcy"),
    ("nabywca_nazwa", "Nabywca"),
    ("netto", "Netto"),
    ("vat", "VAT"),
    ("brutto", "Brutto"),
    ("waluta", "Waluta"),
]


def invoice_record(item: dict, typ: str, date_type: str) -> dict:
    """Buduje rekord faktury z metadanych (do CSV, podsumowań i pobierania)."""
    seller = item.get("seller") or {}
    buyer = item.get("buyer") or {}
    bid = buyer.get("identifier") or {}
    ksef = item.get("ksefNumber") or item.get("KsefNumber") or item.get("ksefReferenceNumber") or ""
    month = month_from_meta(item, date_type)
    if month == UNKNOWN_MONTH and ksef:
        month = month_from_ksef(ksef)
    return {
        "typ": typ,
        "ksef": ksef,
        "miesiac": month,
        "numer": item.get("invoiceNumber") or "",
        "data_wystawienia": item.get("issueDate") or "",
        "sprzedawca_nip": seller.get("nip") or "",
        "sprzedawca_nazwa": seller.get("name") or "",
        "nabywca_nip": bid.get("value") or buyer.get("nip") or "",
        "nabywca_nazwa": buyer.get("name") or "",
        "netto": item.get("netAmount"),
        "vat": item.get("vatAmount"),
        "brutto": item.get("grossAmount"),
        "waluta": item.get("currency") or "",
    }


def _fmt_amount(v) -> str:
    """Kwota z przecinkiem dziesiętnym (format PL, wygodny w Excelu)."""
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return str(v)


def _csv_row(rec: dict) -> dict:
    row = {}
    for key, label in CSV_COLUMNS:
        val = rec.get(key)
        row[label] = _fmt_amount(val) if key in ("netto", "vat", "brutto") else (val or "")
    return row


def write_registry(company_dir: Path, records: list) -> None:
    """Zapisuje rejestr CSV per miesiąc (rejestr_YYYY-MM.csv), scalając po numerze KSeF."""
    if not records:
        return
    labels = [label for _, label in CSV_COLUMNS]
    by_month = defaultdict(list)
    for r in records:
        by_month[r["miesiac"]].append(r)
    for month, recs in by_month.items():
        path = company_dir / f"rejestr_{month}.csv"
        rows = {}
        if path.exists():
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f, delimiter=";"):
                    rows[row.get("Numer KSeF", "")] = row
        for r in recs:
            rows[r["ksef"]] = _csv_row(r)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=labels, delimiter=";")
            w.writeheader()
            for key in sorted(rows, key=lambda k: (rows[k].get("Data wystawienia", ""), k)):
                w.writerow(rows[key])
        log(f"  rejestr: {path.name} ({len(rows)} faktur)")


def log_summary(records: list) -> None:
    """Wypisuje sumy netto/VAT/brutto osobno dla sprzedaży i zakupu."""
    if not records:
        return
    agg = {}
    for r in records:
        a = agg.setdefault(r["typ"], {"n": 0, "netto": 0.0, "vat": 0.0, "brutto": 0.0, "waluta": r.get("waluta") or "PLN"})
        a["n"] += 1
        for k in ("netto", "vat", "brutto"):
            try:
                a[k] += float(r.get(k) or 0)
            except (TypeError, ValueError):
                pass
    for typ in ("sprzedaz", "zakup"):
        if typ in agg:
            a = agg[typ]
            log(f"  {typ}: {a['n']} faktur | netto {a['netto']:.2f} | "
                f"VAT {a['vat']:.2f} | brutto {a['brutto']:.2f} {a['waluta']}")


def merge_month_pdfs(company_dir: Path, records: list) -> None:
    """Scala PDF-y każdego miesiąca w jeden plik do druku: _do_druku/<typ>_<miesiac>.pdf."""
    pairs = sorted({(r["typ"], r["miesiac"]) for r in records})
    if not pairs:
        return
    try:
        from pypdf import PdfWriter
    except ImportError:
        log("  (scalony PDF pominięty — zainstaluj: pip install pypdf)")
        return
    out_root = company_dir / "_do_druku"
    for typ, month in pairs:
        src_dir = company_dir / typ / month
        if not src_dir.exists():
            continue
        pdfs = sorted(p for p in src_dir.glob("*.pdf"))
        if not pdfs:
            continue
        out_root.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        for p in pdfs:
            try:
                writer.append(str(p))
            except Exception as e:
                log(f"    pominięto w scalaniu {p.name}: {e}")
        dest = out_root / f"{typ}_{month}.pdf"
        with open(dest, "wb") as f:
            writer.write(f)
        writer.close()
        log(f"  scalony PDF: _do_druku/{dest.name} ({len(pdfs)} faktur)")


def rsa_oaep_encrypt(public_key, data: bytes) -> bytes:
    """Szyfruje dane kluczem publicznym MF (RSA-OAEP SHA-256)."""
    return public_key.encrypt(
        data,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Odszyfrowuje paczkę KSeF (AES-256-CBC, dopełnienie PKCS#7)."""
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def extract_invoices(data: bytes, out_dir: Path, date_type: str = DEFAULT_DATE_TYPE,
                     fmt: str = DEFAULT_FORMAT, renderer: "PdfRenderer | None" = None,
                     typ: str = "") -> tuple:
    """Rozpakowuje odszyfrowaną paczkę (ZIP lub TarGz) i zapisuje faktury.

    Faktury trafiają do podfolderów miesięcznych 'YYYY-MM' (wg _metadata.json,
    a gdy go brak — wg daty z numeru KSeF). Format 'xml' zapisuje oryginał,
    'pdf' konwertuje wizualizacją MF.
    Zwraca (liczba_nowych, rekordy) — rekordy z metadanych paczki (do CSV/podsumowań).
    """
    # Najpierw wczytaj całą zawartość paczki do pamięci (żeby najpierw poznać
    # _metadata.json, niezależnie od kolejności plików w archiwum).
    entries: dict[str, bytes] = {}
    buf = io.BytesIO(data)
    if zipfile.is_zipfile(buf):
        with zipfile.ZipFile(buf) as zf:
            for name in zf.namelist():
                entries[name] = zf.read(name)
    else:
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:*") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    entries[member.name] = tf.extractfile(member).read()

    records = []
    month_map = {}
    for name, raw in entries.items():
        if os.path.basename(name).lower() == "_metadata.json":
            try:
                meta = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                meta = {}
            for it in meta.get("invoices", []):
                rec = invoice_record(it, typ, date_type)
                if rec["ksef"]:
                    records.append(rec)
                    month_map[rec["ksef"]] = rec["miesiac"]
            break

    ext = "pdf" if fmt == "pdf" else "xml"
    new = 0
    pending = []  # (ksef, xml_bytes, month, dest) — faktury do zapisania
    for name, content in entries.items():
        base = os.path.basename(name)
        if not base or not base.lower().endswith(".xml"):
            continue
        ksef = base[:-4]  # nazwa pliku = "{ksefNumber}.xml"
        month = month_map.get(ksef) or month_from_ksef(ksef)
        target_dir = out_dir / month
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / f"{ksef}.{ext}"
        if dest.exists():
            log(f"    pominięto (już jest) {month}/{dest.name}")
            if fmt == "pdf":
                remove_sibling_xml(dest)
            continue
        pending.append((ksef, content, month, dest))

    if fmt == "pdf":
        if pending:
            log(f"    generuję {len(pending)} PDF (oficjalny generator MF)...")
        pdfs = renderer.render([{"ksef": k, "xml": c} for k, c, _, _ in pending]) if pending else {}
        for ksef, _, month, dest in pending:
            pdf = pdfs.get(ksef)
            if pdf is None:
                log(f"    pominięto (błąd generatora) {month}/{dest.name}")
                continue
            dest.write_bytes(pdf)
            remove_sibling_xml(dest)
            new += 1
            log(f"    zapisano {month}/{dest.name}")
    else:
        for ksef, content, month, dest in pending:
            dest.write_bytes(content)
            new += 1
            log(f"    zapisano {month}/{dest.name}")
    return new, records


# --------------------------------------------------------------------------- #
# KLIENT KSeF
# --------------------------------------------------------------------------- #

class KsefClient:
    def __init__(self, base_url: str, nip: str, token: str):
        self.base = base_url.rstrip("/")
        self.nip = nip
        self.token = token
        self.session = requests.Session()
        self.access_token: str | None = None

    # -- niskopoziomowe wywołania -------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def _headers(self, auth_token: str | None = None) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        tok = auth_token or self.access_token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def _post(self, path: str, json=None, auth_token: str | None = None) -> requests.Response:
        r = self.session.post(self._url(path), json=json,
                              headers=self._headers(auth_token), timeout=HTTP_TIMEOUT_S)
        self._check(r)
        return r

    def _get(self, path: str, auth_token: str | None = None, stream=False) -> requests.Response:
        r = self.session.get(self._url(path), headers=self._headers(auth_token),
                            timeout=HTTP_TIMEOUT_S, stream=stream)
        self._check(r)
        return r

    @staticmethod
    def _check(r: requests.Response) -> None:
        if r.status_code == 429:
            retry = int(r.headers.get("Retry-After", "5"))
            log(f"Limit zapytań (429). Czekam {retry}s...")
            time.sleep(retry)
            raise requests.HTTPError("429 rate limit", response=r)
        if not r.ok:
            raise requests.HTTPError(
                f"{r.request.method} {r.url} -> {r.status_code}\n{r.text[:1000]}",
                response=r,
            )

    # -- uwierzytelnianie ----------------------------------------------------

    def _fetch_certificate(self, usage: str):
        """Pobiera certyfikat MF o danym przeznaczeniu.

        usage: "KsefTokenEncryption" (szyfrowanie tokena) lub
               "SymmetricKeyEncryption" (szyfrowanie klucza AES do eksportu).
        Zwraca krotkę (klucz_publiczny, publicKeyId).
        """
        log(f"Pobieram klucz publiczny MF ({usage})...")
        data = self._get("/security/public-key-certificates").json()
        certs = data if isinstance(data, list) else data.get("certificates", data.get("items", []))
        chosen = None
        for c in certs:
            u = c.get("usage") or c.get("usages") or ""
            if usage in (u if isinstance(u, str) else " ".join(u)):
                chosen = c
                break
        if chosen is None and certs:
            chosen = certs[0]  # fallback: pierwszy dostępny
        if chosen is None:
            sys.exit(f"[BŁĄD] Nie znaleziono certyfikatu o przeznaczeniu {usage}.")
        cert_b64 = chosen.get("certificate") or chosen.get("value")
        cert = load_der_x509_certificate(base64.b64decode(cert_b64))
        key_id = chosen.get("publicKeyId") or chosen.get("certificateId")
        return cert.public_key(), key_id

    def _encrypt_token(self, public_key, timestamp_ms: int) -> str:
        plaintext = f"{self.token}|{timestamp_ms}".encode("utf-8")
        return base64.b64encode(rsa_oaep_encrypt(public_key, plaintext)).decode("ascii")

    @staticmethod
    def _to_ms(ts) -> int:
        """Timestamp z challenge może być liczbą (ms) lub ISO8601 — normalizujemy do ms."""
        if isinstance(ts, (int, float)):
            return int(ts)
        # ISO8601, np. "2026-08-26T10:00:00.8709599+00:00".
        # Uwaga: datetime.fromisoformat (Py<3.11) przyjmuje tylko 3 lub 6 cyfr
        # ułamka sekundy, a demo KSeF potrafi zwrócić 7 — przycinamy do 6.
        s = str(ts).replace("Z", "+00:00")
        m = re.match(r"^(.*\.\d{6})\d*([+-]\d{2}:\d{2})?$", s)
        if m:
            s = m.group(1) + (m.group(2) or "")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)

    def authenticate(self) -> None:
        # 1. challenge
        log("Pobieram challenge...")
        ch = self._post("/auth/challenge").json()
        challenge = ch.get("challenge") or ch.get("Challenge")
        if not challenge:
            sys.exit(f"[BŁĄD] Brak challenge w odpowiedzi: {ch}")
        # Preferuj gotowe milisekundy (demo zwraca "timestampMs"); ISO tylko jako fallback.
        ts_ms_raw = ch.get("timestampMs") or ch.get("TimestampMs")
        if ts_ms_raw is not None:
            ts_ms = int(ts_ms_raw)
        else:
            ts_ms = self._to_ms(ch.get("timestamp") or ch.get("Timestamp"))

        # 2. szyfrowanie tokena
        public_key, _ = self._fetch_certificate("KsefTokenEncryption")
        encrypted = self._encrypt_token(public_key, ts_ms)

        # 3. init z tokenem
        log("Wysyłam żądanie uwierzytelnienia tokenem...")
        body = {
            "challenge": challenge,
            "contextIdentifier": {"type": "Nip", "value": self.nip},
            "encryptedToken": encrypted,
        }
        init = self._post("/auth/ksef-token", json=body).json()
        reference = init.get("referenceNumber") or init.get("ReferenceNumber")
        temp_token = (init.get("authenticationToken") or init.get("AuthenticationToken")
                      or {})
        if isinstance(temp_token, dict):
            temp_token = temp_token.get("token") or temp_token.get("value")
        if not reference or not temp_token:
            sys.exit(f"[BŁĄD] Niepełna odpowiedź /auth/ksef-token: {init}")

        # 4. oczekiwanie na zakończenie uwierzytelnienia
        log("Czekam na zakończenie uwierzytelnienia...")
        deadline = time.monotonic() + POLL_TIMEOUT_S
        while True:
            status = self._get(f"/auth/{reference}", auth_token=temp_token).json()
            code = (status.get("status") or {}).get("code") if isinstance(status.get("status"), dict) else status.get("statusCode")
            desc = (status.get("status") or {}).get("description") if isinstance(status.get("status"), dict) else status.get("description")
            log(f"  status: {code} {desc or ''}")
            # 200 zwykle oznacza sukces; różne środowiska mogą zwracać własne kody
            if code in (200, "200") or (desc and "sukces" in str(desc).lower()):
                break
            if code and str(code).startswith(("4", "5")) and code not in (200, "200"):
                sys.exit(f"[BŁĄD] Uwierzytelnienie odrzucone: {status}")
            if time.monotonic() > deadline:
                sys.exit("[BŁĄD] Przekroczono czas oczekiwania na uwierzytelnienie.")
            time.sleep(POLL_INTERVAL_S)

        # 5. wymiana na accessToken
        log("Pobieram accessToken...")
        redeem = self._post("/auth/token/redeem", auth_token=temp_token).json()
        access = redeem.get("accessToken") or redeem.get("AccessToken")
        if isinstance(access, dict):
            access = access.get("token") or access.get("value")
        if not access:
            sys.exit(f"[BŁĄD] Brak accessToken: {redeem}")
        self.access_token = access
        log("Uwierzytelnienie OK.")

    # -- pobieranie faktur ---------------------------------------------------

    def query_ksef_numbers(self, subject_type: str, typ: str, date_from: str, date_to: str,
                           date_type: str = DEFAULT_DATE_TYPE) -> list:
        """Zwraca listę rekordów faktur (pełne metadane) dla podmiotu i zakresu dat.

        Zakres dat dzielony jest na okna < 3 mies. (limit KSeF), a wyniki
        deduplikowane (na styku okien faktura mogłaby wystąpić dwa razy).
        Każdy rekord ma m.in. 'ksef', 'miesiac' (YYYY-MM), kontrahenta i kwoty.
        typ = 'sprzedaz' | 'zakup'.
        """
        seen: set[str] = set()
        invoices: list = []
        for win_from, win_to in date_windows(date_from, date_to):
            log(f"  {subject_type}: okno {win_from} .. {win_to}")
            offset = 0
            while True:
                body = {
                    "subjectType": subject_type,
                    "dateRange": {
                        "dateType": date_type,
                        "from": f"{win_from}T00:00:00+00:00",
                        "to": f"{win_to}T23:59:59+00:00",
                    },
                }
                r = self._post(
                    f"/invoices/query/metadata?pageOffset={offset}&pageSize={PAGE_SIZE}",
                    json=body,
                ).json()
                items = r.get("invoices") or r.get("items") or r.get("invoiceHeaderList") or []
                for it in items:
                    num = it.get("ksefNumber") or it.get("KsefNumber") or it.get("ksefReferenceNumber")
                    if num and num not in seen:
                        seen.add(num)
                        invoices.append(invoice_record(it, typ, date_type))
                got = len(items)
                log(f"    strona offset={offset}, pobrano {got} pozycji")
                has_more = r.get("hasMore")
                if has_more is None:
                    has_more = got == PAGE_SIZE
                if not has_more or got == 0:
                    break
                offset += PAGE_SIZE
        return invoices

    def fetch_invoice_xml(self, ksef_number: str) -> bytes:
        """Pobiera i zwraca bajty XML faktury po numerze KSeF."""
        return self._get(f"/invoices/ksef/{ksef_number}").content

    # -- eksport asynchroniczny (paczka ZIP) --------------------------------

    def start_export(self, subject_type: str, date_from: str, date_to: str,
                     date_type: str = DEFAULT_DATE_TYPE):
        """Zleca eksport zaszyfrowanej paczki faktur.

        Generuje klucz AES-256 + IV, szyfruje klucz RSA-OAEP kluczem MF i wysyła
        żądanie. Zwraca (referenceNumber, aes_key, iv) — klucz i IV są potrzebne
        do późniejszego odszyfrowania paczki.
        """
        public_key, key_id = self._fetch_certificate("SymmetricKeyEncryption")
        aes_key = os.urandom(32)
        iv = os.urandom(16)
        encryption = {
            "encryptedSymmetricKey": base64.b64encode(rsa_oaep_encrypt(public_key, aes_key)).decode("ascii"),
            "initializationVector": base64.b64encode(iv).decode("ascii"),
        }
        if key_id:
            encryption["publicKeyId"] = key_id
        # Eksport oczekuje SubjectType z wielkiej litery (Subject1/Subject2).
        subj = subject_type[:1].upper() + subject_type[1:]
        body = {
            "encryption": encryption,
            "filters": {
                "subjectType": subj,
                "dateRange": {
                    "dateType": date_type,
                    "from": f"{date_from}T00:00:00+00:00",
                    "to": f"{date_to}T23:59:59+00:00",
                },
            },
            "compressionType": "Zip",
        }
        r = self._post("/invoices/exports", json=body).json()
        reference = r.get("referenceNumber") or r.get("ReferenceNumber")
        if not reference:
            sys.exit(f"[BŁĄD] Brak referenceNumber w odpowiedzi eksportu: {r}")
        return reference, aes_key, iv

    def wait_export(self, reference_number: str) -> dict:
        """Odpytuje status eksportu aż do zakończenia. Zwraca dane paczki (package)."""
        deadline = time.monotonic() + EXPORT_POLL_TIMEOUT_S
        while True:
            st = self._get(f"/invoices/exports/{reference_number}").json()
            status = st.get("status") or {}
            code = status.get("code")
            desc = status.get("description")
            log(f"  eksport status: {code} {desc or ''}")
            if code in (200, "200"):
                return st.get("package") or {}
            if code not in (100, "100"):
                sys.exit(f"[BŁĄD] Eksport nie powiódł się: {status}")
            if time.monotonic() > deadline:
                sys.exit("[BŁĄD] Przekroczono czas oczekiwania na eksport paczki.")
            time.sleep(POLL_INTERVAL_S)

    def download_package(self, package: dict, aes_key: bytes, iv: bytes, out_dir: Path,
                         date_type: str = DEFAULT_DATE_TYPE,
                         fmt: str = DEFAULT_FORMAT, renderer: "PdfRenderer | None" = None,
                         typ: str = "") -> tuple:
        """Pobiera części paczki, weryfikuje, odszyfrowuje i rozpakowuje faktury.

        Zwraca (liczba_nowych, rekordy).
        """
        parts = sorted(package.get("parts", []), key=lambda p: p.get("ordinalNumber", 0))
        if not parts:
            return 0, []
        blob = bytearray()
        for p in parts:
            name = p.get("partName")
            log(f"    pobieram część {p.get('ordinalNumber')} ({name}, {p.get('encryptedPartSize')} B)")
            method = (p.get("method") or "GET").upper()
            # Link do części nie wymaga tokenu dostępowego (osobny storage).
            r = requests.request(method, p["url"], timeout=HTTP_TIMEOUT_S)
            r.raise_for_status()
            data = r.content
            expected = p.get("encryptedPartHash")
            if expected:
                got = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
                if got != expected:
                    sys.exit(f"[BŁĄD] Niezgodny skrót zaszyfrowanej części {name}.")
            blob.extend(data)
        plaintext = aes_cbc_decrypt(aes_key, iv, bytes(blob))
        return extract_invoices(plaintext, out_dir, date_type, fmt, renderer, typ)


# --------------------------------------------------------------------------- #
# WIZUALIZACJA / PDF
# --------------------------------------------------------------------------- #

def _find_node() -> "str | None":
    """Znajduje interpreter Node: KSEF_NODE, potem PATH, potem typowe lokalizacje.

    Obejmuje macOS (Homebrew: Apple Silicon /opt/homebrew, Intel /usr/local),
    Linux oraz instalację Windows widoczną z WSL.
    """
    override = os.environ.get("KSEF_NODE")
    if override:
        return override
    found = shutil.which("node")
    if found:
        return found
    for cand in (
        "/opt/homebrew/bin/node",              # macOS (Apple Silicon, Homebrew)
        "/usr/local/bin/node",                 # macOS (Intel) / Linux
        "/usr/bin/node",                       # Linux
        f"{os.path.expanduser('~')}/.nvm/current/bin/node",  # nvm
        "/mnt/c/Program Files/nodejs/node.exe",              # Windows z WSL
        "/mnt/c/Program Files (x86)/nodejs/node.exe",
    ):
        if Path(cand).exists():
            return cand
    return None


class PdfRenderer:
    """Generuje PDF faktury OFICJALNYM generatorem MF (CIRFMF/ksef-pdf-generator).

    Generator to biblioteka Node (pdfmake). Wołamy go wsadowo przez lokalny
    skrypt `convert-cli.cjs`: przekazujemy faktury jako base64 w pliku JSON i
    odbieramy PDF-y jako base64 (bez ścieżek zewnętrznych — odporne na WSL/Windows).
    Dzięki temu wynik jest identyczny z PDF pobieranym z Aplikacji Podatnika.
    """

    def __init__(self, env: str, vendor_dir: Path = VENDOR_DIR):
        self.env = env
        self.vendor_dir = vendor_dir
        self.qr_host = QR_HOSTS.get(env, QR_HOSTS["test"])
        self._node = None

    def _ensure_ready(self) -> None:
        if self._node is not None:
            return
        node = _find_node()
        if not node:
            sys.exit("[BŁĄD] Tryb PDF wymaga Node.js. Zainstaluj Node (>=20) lub wskaż "
                     "go zmienną KSEF_NODE=/ścieżka/do/node.")
        if not (self.vendor_dir / "src" / "index.ts").exists():
            sys.exit(f"[BŁĄD] Brak generatora PDF w {self.vendor_dir}. Zobacz README "
                     f"(sekcja PDF): sklonuj CIRFMF/ksef-pdf-generator i uruchom 'npm install'.")
        if not (self.vendor_dir / "node_modules").exists():
            sys.exit(f"[BŁĄD] Brak zależności generatora. Wejdź do {self.vendor_dir} "
                     f"i uruchom 'npm install'.")
        # Wstrzyknij (odśwież) wsadowy konwerter do katalogu generatora.
        (self.vendor_dir / PDF_CLI).write_text(PDF_CLI_SOURCE, encoding="utf-8")
        self._node = node

    def _qr_and_acdate(self, ksef_number: str, xml_bytes: bytes):
        """Buduje link weryfikacyjny QR oraz datę nadania numeru KSeF z numeru + XML."""
        parts = ksef_number.split("-")
        nip = parts[0] if parts else ""
        ymd = parts[1] if len(parts) > 1 else "00000000"
        digest = hashlib.sha256(xml_bytes).digest()
        h = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        qr = f"{self.qr_host}/invoice/{nip}/{ymd[6:8]}-{ymd[4:6]}-{ymd[0:4]}/{h}"
        ac_date = f"{ymd[6:8]}.{ymd[4:6]}.{ymd[0:4]}"
        return qr, ac_date

    def render(self, jobs: list) -> dict:
        """jobs: [{'ksef': str, 'xml': bytes}]. Zwraca {ksef: pdf_bytes} dla udanych."""
        if not jobs:
            return {}
        self._ensure_ready()
        items = []
        for j in jobs:
            qr, ac_date = self._qr_and_acdate(j["ksef"], j["xml"])
            items.append({
                "id": j["ksef"],
                "xmlB64": base64.b64encode(j["xml"]).decode("ascii"),
                "nrKSeF": j["ksef"],
                "acDate": ac_date,
                "qrCode": qr,
            })
        job_path = self.vendor_dir / "job.json"
        result_path = self.vendor_dir / "job.json.result"
        job_path.write_text(json.dumps({"items": items}), encoding="utf-8")
        if result_path.exists():
            result_path.unlink()
        try:
            proc = subprocess.run(
                [self._node, PDF_CLI, "job.json"],
                cwd=str(self.vendor_dir),
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=max(120, 15 * len(items)),
            )
        finally:
            if job_path.exists():
                job_path.unlink()
        if not result_path.exists():
            sys.exit(f"[BŁĄD] Generator PDF nie zwrócił wyniku.\n{proc.stderr[-1500:]}")
        data = json.loads(result_path.read_text(encoding="utf-8"))
        result_path.unlink()
        out = {}
        for r in data.get("results", []):
            if "pdfB64" in r:
                out[r["id"]] = base64.b64decode(r["pdfB64"])
            else:
                log(f"    BŁĄD generatora PDF {r.get('id')}: {r.get('error')}")
        return out


# --------------------------------------------------------------------------- #
# GŁÓWNY PRZEBIEG
# --------------------------------------------------------------------------- #

def retry(fn, tries=4):
    """Prosty retry dla 429/przejściowych błędów sieci."""
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except requests.HTTPError as e:
            if attempt == tries:
                raise
            log(f"  ponawiam ({attempt}/{tries}): {e}")
            time.sleep(2 * attempt)
        except requests.RequestException as e:
            if attempt == tries:
                raise
            log(f"  błąd sieci, ponawiam ({attempt}/{tries}): {e}")
            time.sleep(2 * attempt)


ZADANIA = [
    ("sprzedaz", SUBJECT_SPRZEDAZ),
    ("zakup", SUBJECT_ZAKUP),
]


def run_single(client: "KsefClient", out_dir: Path, date_from: str, date_to: str, date_type: str,
               fmt: str = DEFAULT_FORMAT, renderer: "PdfRenderer | None" = None):
    """Tryb pojedynczy: metadane -> pobieranie faktura po fakturze.

    Zwraca (liczba_znalezionych, liczba_nowych, rekordy).
    """
    ext = "pdf" if fmt == "pdf" else "xml"
    total_found = 0
    total_new = 0
    records: list = []
    for folder, subject in ZADANIA:
        out = out_dir / folder
        out.mkdir(parents=True, exist_ok=True)
        log(f"== Faktury: {folder} ==")
        invoices = retry(lambda s=subject, t=folder: client.query_ksef_numbers(s, t, date_from, date_to, date_type))
        records.extend(invoices)
        total_found += len(invoices)
        log(f"Znaleziono {len(invoices)} faktur ({folder}).")
        pdf_jobs = []  # (num, dest, rel, xml_bytes) — do wsadowej konwersji PDF
        for i, rec in enumerate(invoices, 1):
            num, month = rec["ksef"], rec["miesiac"]
            month_dir = out / month
            month_dir.mkdir(parents=True, exist_ok=True)
            dest = month_dir / f"{num}.{ext}"
            rel = f"{month}/{dest.name}"
            if dest.exists():
                log(f"  [{i}/{len(invoices)}] pominięto (już jest) {rel}")
                if fmt == "pdf":
                    remove_sibling_xml(dest)
                continue
            xml_bytes = retry(lambda n=num: client.fetch_invoice_xml(n))
            if fmt == "pdf":
                pdf_jobs.append((num, dest, rel, xml_bytes))
            else:
                dest.write_bytes(xml_bytes)
                total_new += 1
                log(f"  [{i}/{len(invoices)}] zapisano {rel}")

        if fmt == "pdf" and pdf_jobs:
            log(f"  Generuję {len(pdf_jobs)} PDF (oficjalny generator MF)...")
            pdfs = renderer.render([{"ksef": n, "xml": x} for n, _, _, x in pdf_jobs])
            for num, dest, rel, _ in pdf_jobs:
                pdf = pdfs.get(num)
                if pdf is None:
                    log(f"    pominięto (błąd generatora) {rel}")
                    continue
                dest.write_bytes(pdf)
                remove_sibling_xml(dest)
                total_new += 1
                log(f"    zapisano {rel}")
    return total_found, total_new, records


def run_export(client: "KsefClient", out_dir: Path, date_from: str, date_to: str, date_type: str,
               fmt: str = DEFAULT_FORMAT, renderer: "PdfRenderer | None" = None):
    """Tryb eksportu: asynchroniczna, zaszyfrowana paczka ZIP na okno dat.

    Zwraca (liczba_znalezionych, liczba_nowych, rekordy).
    """
    total_found = 0
    total_new = 0
    records: list = []
    for folder, subject in ZADANIA:
        out = out_dir / folder
        out.mkdir(parents=True, exist_ok=True)
        log(f"== Eksport paczki: {folder} ==")
        for win_from, win_to in date_windows(date_from, date_to):
            log(f"  {subject}: okno {win_from} .. {win_to}")
            reference, aes_key, iv = retry(
                lambda s=subject, a=win_from, b=win_to: client.start_export(s, a, b, date_type))
            log(f"  referenceNumber={reference}")
            package = client.wait_export(reference)
            count = package.get("invoiceCount", 0)
            total_found += count
            log(f"  faktur w paczce: {count} | truncated={package.get('isTruncated')}")
            if package.get("isTruncated"):
                log("  UWAGA: paczka ucięta (limit 10000 faktur / rozmiaru) — "
                    "zawęź zakres dat, część faktur mogła zostać pominięta.")
            if count:
                new, recs = retry(
                    lambda pkg=package, k=aes_key, v=iv, o=out, t=folder:
                    client.download_package(pkg, k, v, o, date_type, fmt, renderer, t))
                total_new += new
                records.extend(recs)
    return total_found, total_new, records


def load_companies(here: Path) -> list:
    """Wczytuje listę firm z firmy.json; awaryjnie pojedynczą firmę z .env.

    Każda firma: {nazwa, nip, token, env}. env domyślnie z KSEF_ENV / 'prod'.
    """
    default_env = os.environ.get("KSEF_ENV", "prod").lower()
    cfg = here / "firmy.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except ValueError as e:
            sys.exit(f"[BŁĄD] Niepoprawny firmy.json: {e}")
        firmy = data.get("firmy") if isinstance(data, dict) else data
        out = []
        for f in firmy or []:
            token = f.get("token") or f.get("KSEF_TOKEN")
            nip = str(f.get("nip") or f.get("KSEF_NIP") or "")
            if not token or not nip:
                continue
            out.append({
                "nazwa": f.get("nazwa") or f.get("name") or nip,
                "nip": nip,
                "token": token,
                "env": (f.get("env") or default_env).lower(),
            })
        if not out:
            sys.exit("[BŁĄD] firmy.json nie zawiera żadnej firmy z 'token' i 'nip'.")
        return out
    # Awaryjnie: pojedyncza firma z .env
    token = os.environ.get("KSEF_TOKEN")
    nip = os.environ.get("KSEF_NIP")
    if token and nip:
        return [{"nazwa": os.environ.get("KSEF_FIRMA_NAZWA") or nip,
                 "nip": nip, "token": token, "env": default_env}]
    sys.exit("[BŁĄD] Brak konfiguracji firm. Utwórz firmy.json (wzór: firmy.example.json) "
             "albo uzupełnij KSEF_TOKEN i KSEF_NIP w .env.")


def choose_companies(companies: list) -> list:
    """Zwraca listę firm do przetworzenia: przez KSEF_FIRMA (numer/nazwa/'all'),
    automatycznie gdy jedna, inaczej interaktywne menu (z opcją 'wszystkie')."""
    pre = os.environ.get("KSEF_FIRMA", "").strip()
    if pre:
        if pre.lower() in ("all", "wszystkie", "*", "0"):
            return list(companies)
        if pre.isdigit() and 1 <= int(pre) <= len(companies):
            return [companies[int(pre) - 1]]
        for c in companies:
            if c["nazwa"].lower() == pre.lower():
                return [c]
        for c in companies:
            if pre.lower() in c["nazwa"].lower():
                return [c]
        sys.exit(f"[BŁĄD] Nie znaleziono firmy pasującej do KSEF_FIRMA={pre!r}.")
    if len(companies) == 1:
        return [companies[0]]
    if not sys.stdin.isatty():
        sys.exit("[BŁĄD] Wiele firm w konfiguracji — wskaż firmę zmienną KSEF_FIRMA "
                 "(numer, nazwa lub 'all').")
    print("Wybierz firmę:")
    for i, c in enumerate(companies, 1):
        print(f"  {i}) {c['nazwa']}  (NIP {c['nip']}, {c['env']})")
    print("  0) Wszystkie firmy")
    while True:
        s = input("Numer firmy: ").strip()
        if s == "0":
            return list(companies)
        if s.isdigit() and 1 <= int(s) <= len(companies):
            return [companies[int(s) - 1]]
        print("Niepoprawny numer — spróbuj ponownie.")


def choose_dates() -> tuple:
    """Zakres dat. Interaktywnie pyta (domyślnie: bieżący miesiąc = 'nowe z tego
    miesiąca'). Nieinteraktywnie: bierze z env lub bieżący miesiąc."""
    today = datetime.now(timezone.utc).date()
    first = today.replace(day=1)
    def_from, def_to = first.isoformat(), today.isoformat()

    if not sys.stdin.isatty():
        return (os.environ.get("KSEF_DATE_FROM") or def_from,
                os.environ.get("KSEF_DATE_TO") or def_to)

    def ask(label: str, default: str) -> str:
        while True:
            s = input(f"  {label} [{default}]: ").strip()
            if not s:
                return default
            try:
                date.fromisoformat(s)
                return s
            except ValueError:
                print("    Zły format — użyj RRRR-MM-DD (albo Enter dla domyślnej).")

    print("Zakres dat (Enter = nowe z bieżącego miesiąca):")
    return ask("Data od", def_from), ask("Data do", def_to)


def company_slug(name: str) -> str:
    """Bezpieczna nazwa folderu dla firmy."""
    s = re.sub(r'[\\/:*?"<>|]+', "", name).strip()
    s = re.sub(r"\s+", "_", s)
    return s or "firma"


def process_company(company: dict, date_from: str, date_to: str, date_type: str,
                    mode: str, fmt: str) -> tuple:
    """Przetwarza jedną firmę: pobiera faktury, zapisuje rejestr CSV, scala PDF-y,
    wypisuje podsumowanie. Zwraca (znalezione, nowe)."""
    env = company["env"]
    if env not in BASE_URLS:
        sys.exit(f"[BŁĄD] Nieznane środowisko '{env}' dla firmy {company['nazwa']}. "
                 f"Dozwolone: {list(BASE_URLS)}")
    base_url = BASE_URLS[env]
    out_dir = FAKTURY_DIR / company_slug(company["nazwa"])
    renderer = PdfRenderer(env) if fmt == "pdf" else None

    log("=" * 60)
    log(f"Firma: {company['nazwa']} (NIP {company['nip']}) | środowisko: {env}")
    log(f"Zakres dat: {date_from} .. {date_to} | dateType: {date_type} "
        f"| tryb: {mode} | format: {fmt}")

    client = KsefClient(base_url, company["nip"], company["token"])
    retry(client.authenticate)

    if mode in MODES_EXPORT:
        total_found, total_new, records = run_export(
            client, out_dir, date_from, date_to, date_type, fmt, renderer)
    else:
        total_found, total_new, records = run_single(
            client, out_dir, date_from, date_to, date_type, fmt, renderer)

    write_registry(out_dir, records)
    if fmt == "pdf":
        merge_month_pdfs(out_dir, records)

    ext = "pdf" if fmt == "pdf" else "xml"
    total_on_disk = sum(1 for p in out_dir.rglob(f"*.{ext}")
                        if "_do_druku" not in p.parts) if out_dir.exists() else 0
    log(f"Podsumowanie ({company['nazwa']}):")
    log_summary(records)
    log(f"Pobrano {total_found} faktur, nowych: {total_new} szt. | na dysku: {total_on_disk}")
    log(f"Folder: {out_dir}")
    return total_found, total_new


def main() -> None:
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env")

    date_type = normalize_date_type(os.environ.get("KSEF_DATE_TYPE", DEFAULT_DATE_TYPE))
    mode = os.environ.get("KSEF_MODE", "single").strip().lower()
    if mode not in MODES_SINGLE | MODES_EXPORT:
        sys.exit(f"[BŁĄD] Nieznany KSEF_MODE={mode!r}. Dozwolone: single | export.")
    fmt = os.environ.get("KSEF_FORMAT", DEFAULT_FORMAT).strip().lower()
    if fmt not in FORMATS:
        sys.exit(f"[BŁĄD] Nieznany KSEF_FORMAT={fmt!r}. Dozwolone: xml | pdf.")

    companies = load_companies(here)
    selected = choose_companies(companies)
    date_from, date_to = choose_dates()

    grand_found = grand_new = 0
    for company in selected:
        found, new = process_company(company, date_from, date_to, date_type, mode, fmt)
        grand_found += found
        grand_new += new

    if len(selected) > 1:
        log("=" * 60)
        log(f"RAZEM ({len(selected)} firm): pobrano {grand_found} faktur, "
            f"nowych: {grand_new} szt.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nPrzerwano.")
