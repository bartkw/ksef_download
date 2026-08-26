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
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import zipfile
from calendar import monthrange
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

PAGE_SIZE = 100
POLL_INTERVAL_S = 2          # co ile sekund odpytywać status uwierzytelnienia
POLL_TIMEOUT_S = 120         # maks. czas oczekiwania na uwierzytelnienie
EXPORT_POLL_TIMEOUT_S = 600  # eksport paczki bywa dłuższy niż uwierzytelnienie
HTTP_TIMEOUT_S = 60

OUT_DIR = Path(__file__).resolve().parent / "faktury"


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


def _month_map_from_metadata(raw: bytes, date_type: str) -> dict:
    """Buduje mapę {ksefNumber: 'YYYY-MM'} z pliku _metadata.json paczki."""
    mapping = {}
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return mapping
    for inv in meta.get("invoices", []):
        num = inv.get("ksefNumber")
        if num:
            mapping[num] = month_from_meta(inv, date_type)
    return mapping


def extract_invoices(data: bytes, out_dir: Path, date_type: str = DEFAULT_DATE_TYPE) -> int:
    """Rozpakowuje odszyfrowaną paczkę (ZIP lub TarGz) — zapisuje pliki XML.

    Faktury trafiają do podfolderów miesięcznych 'YYYY-MM' (wg _metadata.json,
    a gdy go brak — wg daty z numeru KSeF). Zwraca liczbę NOWO zapisanych faktur.
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

    month_map = {}
    for name, raw in entries.items():
        if os.path.basename(name).lower() == "_metadata.json":
            month_map = _month_map_from_metadata(raw, date_type)
            break

    new = 0
    for name, content in entries.items():
        base = os.path.basename(name)
        if not base or not base.lower().endswith(".xml"):
            continue
        ksef = base[:-4]  # nazwa pliku = "{ksefNumber}.xml"
        month = month_map.get(ksef) or month_from_ksef(ksef)
        target_dir = out_dir / month
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / base
        if dest.exists():
            log(f"    pominięto (już jest) {month}/{base}")
            continue
        dest.write_bytes(content)
        new += 1
        log(f"    zapisano {month}/{base}")
    return new


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

    def query_ksef_numbers(self, subject_type: str, date_from: str, date_to: str,
                           date_type: str = DEFAULT_DATE_TYPE) -> list:
        """Zwraca listę faktur (num, folder_miesiąca) dla typu podmiotu i zakresu dat.

        Zakres dat dzielony jest na okna < 3 mies. (limit KSeF), a wyniki
        deduplikowane (na styku okien faktura mogłaby wystąpić dwa razy).
        Folder miesiąca ('YYYY-MM') liczony wg wybranego date_type.
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
                        month = month_from_meta(it, date_type)
                        if month == UNKNOWN_MONTH:
                            month = month_from_ksef(num)
                        invoices.append((num, month))
                got = len(items)
                log(f"    strona offset={offset}, pobrano {got} pozycji")
                has_more = r.get("hasMore")
                if has_more is None:
                    has_more = got == PAGE_SIZE
                if not has_more or got == 0:
                    break
                offset += PAGE_SIZE
        return invoices

    def download_invoice(self, ksef_number: str, dest: Path) -> bool:
        """Pobiera XML faktury po numerze KSeF. Zwraca True jeśli pobrano (False = już istniała)."""
        if dest.exists():
            return False
        r = self._get(f"/invoices/ksef/{ksef_number}")
        dest.write_bytes(r.content)
        return True

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
                         date_type: str = DEFAULT_DATE_TYPE) -> int:
        """Pobiera części paczki, weryfikuje, odszyfrowuje i rozpakowuje XML-e.

        Zwraca liczbę nowo zapisanych faktur.
        """
        parts = sorted(package.get("parts", []), key=lambda p: p.get("ordinalNumber", 0))
        if not parts:
            return 0
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
        return extract_invoices(plaintext, out_dir, date_type)


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


def run_single(client: "KsefClient", date_from: str, date_to: str, date_type: str):
    """Tryb pojedynczy: metadane -> pobieranie faktura po fakturze.

    Zwraca (liczba_znalezionych, liczba_nowych).
    """
    total_found = 0
    total_new = 0
    for folder, subject in ZADANIA:
        out = OUT_DIR / folder
        out.mkdir(parents=True, exist_ok=True)
        log(f"== Faktury: {folder} ==")
        invoices = retry(lambda s=subject: client.query_ksef_numbers(s, date_from, date_to, date_type))
        total_found += len(invoices)
        log(f"Znaleziono {len(invoices)} faktur ({folder}).")
        for i, (num, month) in enumerate(invoices, 1):
            month_dir = out / month
            month_dir.mkdir(parents=True, exist_ok=True)
            dest = month_dir / f"{num}.xml"
            new = retry(lambda n=num, d=dest: client.download_invoice(n, d))
            rel = f"{month}/{dest.name}"
            if new:
                total_new += 1
                log(f"  [{i}/{len(invoices)}] zapisano {rel}")
            else:
                log(f"  [{i}/{len(invoices)}] pominięto (już jest) {rel}")
    return total_found, total_new


def run_export(client: "KsefClient", date_from: str, date_to: str, date_type: str):
    """Tryb eksportu: asynchroniczna, zaszyfrowana paczka ZIP na okno dat.

    Zwraca (liczba_znalezionych, liczba_nowych).
    """
    total_found = 0
    total_new = 0
    for folder, subject in ZADANIA:
        out = OUT_DIR / folder
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
                total_new += retry(
                    lambda pkg=package, k=aes_key, v=iv, o=out:
                    client.download_package(pkg, k, v, o, date_type))
    return total_found, total_new


def main() -> None:
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env")

    env = os.environ.get("KSEF_ENV", "prod").lower()
    if env not in BASE_URLS:
        sys.exit(f"[BŁĄD] Nieznane środowisko KSEF_ENV={env}. Dozwolone: {list(BASE_URLS)}")
    base_url = BASE_URLS[env]

    token = require_env("KSEF_TOKEN")
    nip = require_env("KSEF_NIP")

    today = datetime.now(timezone.utc).date()
    date_from = os.environ.get("KSEF_DATE_FROM") or (today - timedelta(days=30)).isoformat()
    date_to = os.environ.get("KSEF_DATE_TO") or today.isoformat()
    date_type = normalize_date_type(os.environ.get("KSEF_DATE_TYPE", DEFAULT_DATE_TYPE))

    mode = os.environ.get("KSEF_MODE", "single").strip().lower()
    if mode not in MODES_SINGLE | MODES_EXPORT:
        sys.exit(f"[BŁĄD] Nieznany KSEF_MODE={mode!r}. Dozwolone: single | export.")

    log(f"Środowisko: {env} ({base_url})")
    log(f"NIP: {nip} | zakres dat: {date_from} .. {date_to} | dateType: {date_type} | tryb: {mode}")

    client = KsefClient(base_url, nip, token)
    retry(client.authenticate)

    if mode in MODES_EXPORT:
        total_found, total_new = run_export(client, date_from, date_to, date_type)
    else:
        total_found, total_new = run_single(client, date_from, date_to, date_type)

    # Łączna liczba faktur trzymanych na dysku (wszystkie dotychczas pobrane).
    total_on_disk = sum(1 for _ in OUT_DIR.rglob("*.xml")) if OUT_DIR.exists() else 0
    log("=" * 60)
    log(f"Pobrano {total_found} faktur, nowych: {total_new} szt.")
    log(f"Wszystkie faktury ({total_on_disk} szt.) trzymane są w folderze: {OUT_DIR}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nPrzerwano.")
