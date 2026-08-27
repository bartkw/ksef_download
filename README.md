# Pobieranie faktur z KSeF 2.0

Skrypt w Pythonie pobierający faktury (sprzedażowe i zakupowe) z Krajowego
Systemu e-Faktur (KSeF 2.0) przy użyciu **tokena KSeF**.

## Wymagania

- Python 3.10+
- Token KSeF wygenerowany w Aplikacji Podatnika KSeF (w kontekście Twojej firmy)

## Instalacja

**Szybko (macOS / Linux)** — jednym poleceniem:

```bash
bash setup.sh
```

`setup.sh` wykonuje kolejno:

1. sprawdza `python3` (3.10+),
2. instaluje zależności Pythona (`requests`, `cryptography`) — z fallbackami
   dla środowisk „externally-managed" (`--user` / `--break-system-packages`,
   a w ostateczności podpowiada `venv`),
3. wykrywa Node.js (Homebrew Apple Silicon `/opt/homebrew`, Intel `/usr/local`,
   `PATH`, nvm) — wymagany tylko dla `KSEF_FORMAT=pdf`,
4. klonuje generator PDF `CIRFMF/ksef-pdf-generator` (jeśli go brak)
   i uruchamia `npm install` **na tej maszynie**,
5. tworzy `.env` z `env.example.txt` (istniejącego nie nadpisuje),
6. robi self-check (moduł się ładuje, Node wykryty, generator gotowy).

Po zakończeniu uzupełnij `.env` (`KSEF_TOKEN`, `KSEF_NIP`) i uruchom skrypt.
Bez Node.js tryb `pdf` nie zadziała, ale tryb `xml` owszem.

**Ręcznie:**

```bash
pip install -r requirements.txt
# dla KSEF_FORMAT=pdf dodatkowo: Node.js + generator (patrz sekcja „Format zapisu")
```

## Konfiguracja

1. Skopiuj `env.example.txt` do pliku `.env`.
2. Uzupełnij:
   - `KSEF_TOKEN` – Twój token KSeF (traktuj jak hasło!),
   - `KSEF_NIP` – NIP firmy,
   - `KSEF_ENV` – `prod` / `demo` / `test`,
   - `KSEF_DATE_FROM`, `KSEF_DATE_TO` – zakres dat (opcjonalnie; domyślnie ostatnie 30 dni),
   - `KSEF_DATE_TYPE` – typ daty filtrowania: `Issue` (wystawienia, domyślnie),
     `Invoicing` (przyjęcia w KSeF) lub `PermanentStorage` (trwałego zapisu),
   - `KSEF_MODE` – tryb pobierania: `single` (domyślnie) lub `export` (patrz niżej),
   - `KSEF_FORMAT` – format zapisu: `xml` (oryginał, domyślnie) lub `pdf`
     (wizualizacja do druku oficjalnym generatorem MF; wymaga Node.js + generatora
     — patrz sekcja „Format zapisu", instaluje `setup.sh`).

> ⚠️ Nie commituj pliku `.env` ani tokena do repozytorium.

## Uruchomienie

```bash
python ksef_pobierz.py
```

Faktury (pliki XML) trafią do podfolderów **rok-miesiąc** (`YYYY-MM`),
w podziale na sprzedaż i zakup:

```
faktury/
├── sprzedaz/            # faktury wystawione przez Twoją firmę (subject1)
│   ├── 2026-07/         # faktury z lipca 2026
│   └── 2026-08/
└── zakup/               # faktury wystawione na Twoją firmę (subject2)
    └── 2026-08/
```

Miesiąc ustalany jest według wybranego `KSEF_DATE_TYPE` (domyślnie data
wystawienia). Skrypt pomija faktury już zapisane na dysku (po numerze KSeF),
więc można go uruchamiać wielokrotnie – działa przyrostowo.

## Format zapisu (`KSEF_FORMAT`)

- **`xml`** (domyślny) – zapisuje oryginalny plik XML faktury.
- **`pdf`** – generuje **PDF do druku identyczny z tym z Aplikacji Podatnika
  KSeF**. Faktura KSeF to dokument XML (API nie udostępnia PDF-ów), więc skrypt
  konwertuje ją **oficjalnym generatorem MF**
  [`CIRFMF/ksef-pdf-generator`](https://github.com/CIRFMF/ksef-pdf-generator)
  (ten sam, którego używają wystawcy) uruchamianym lokalnie przez Node.js.
  Skrypt liczy też link weryfikacyjny QR (`qr.ksef.mf.gov.pl`).

  Pliki zapisywane są jako `{numerKSeF}.pdf`. XML nie jest trzymany: nowe faktury
  konwertowane są w pamięci, a ewentualny istniejący `{numerKSeF}.xml` (np. z
  wcześniejszych uruchomień w trybie `xml`) jest usuwany po zapisaniu PDF.
  Jeśli konwersja pojedynczej faktury się nie powiedzie, skrypt to zgłasza
  i kontynuuje z pozostałymi.

### Instalacja generatora PDF (jednorazowo, na każdej maszynie)

Tryb `pdf` wymaga **Node.js ≥ 20** oraz sklonowanego generatora MF:

```bash
# w katalogu projektu:
git clone https://github.com/CIRFMF/ksef-pdf-generator vendor/ksef-pdf-generator
cd vendor/ksef-pdf-generator
npm install
```

To wszystko — wsadowy konwerter (`convert-cli.cjs`) skrypt sam dopisze do tego
katalogu przy pierwszym uruchomieniu w trybie `pdf`.

> **Ważne przy przenoszeniu między systemami (np. Windows ↔ macOS):**
> katalog `node_modules` jest zależny od systemu. Nie kopiuj go między
> maszynami — na nowym komputerze wejdź do `vendor/ksef-pdf-generator/`
> i uruchom `npm install` jeszcze raz (jest w `.gitignore`, więc i tak nie
> trafi do repo).

**macOS:** zainstaluj Node przez Homebrew (`brew install node`) — skrypt sam go
znajdzie (Apple Silicon `/opt/homebrew`, Intel `/usr/local`). Zależności Pythona:
`pip3 install requests cryptography`. Uruchomienie: `python3 ksef_pobierz.py`.
Jeśli `node` nie jest w `PATH`, wskaż go zmienną `KSEF_NODE=/ścieżka/do/node`.

## Tryby pobierania (`KSEF_MODE`)

- **`single`** (domyślny) – dla każdej faktury osobne `GET /invoices/ksef/{num}`.
  Prosty i wystarczający dla typowej firmy.
- **`export`** – asynchroniczny eksport **zaszyfrowanej paczki ZIP**
  (`POST /invoices/exports`). Zalecany do **masowego** pobierania dużych
  wolumenów. Skrypt:
  1. generuje klucz AES-256 + IV, szyfruje klucz kluczem publicznym MF
     (RSA-OAEP SHA-256, certyfikat `SymmetricKeyEncryption`),
  2. zleca eksport i odpytuje status aż do zakończenia (`200`),
  3. pobiera części paczki (bez tokenu – osobny storage), weryfikuje ich skróty
     SHA-256, odszyfrowuje (AES-256-CBC) i rozpakowuje pliki XML do `faktury/`.

  Uwaga: jedna paczka mieści do **10 000 faktur**; przy przekroczeniu limitu
  (`isTruncated`) skrypt to zgłasza – zawęź wtedy zakres dat.

## Jak to działa (KSeF 2.0)

1. `POST /auth/challenge` – pobranie wyzwania i znacznika czasu.
2. Szyfrowanie `token|timestamp` kluczem publicznym MF (RSA-OAEP SHA-256).
3. `POST /auth/ksef-token` – uwierzytelnienie tokenem.
4. `GET /auth/{referenceNumber}` – oczekiwanie na sukces.
5. `POST /auth/token/redeem` – pobranie `accessToken` (Bearer).
6. `POST /invoices/query/metadata` – lista numerów KSeF (osobno sprzedaż/zakup).
7. `GET /invoices/ksef/{ksefNumber}` – pobranie XML każdej faktury.

W trybie `export` kroki 6–7 zastępuje: `POST /invoices/exports` (ze zaszyfrowanym
kluczem AES) → `GET /invoices/exports/{referenceNumber}` (poll statusu) →
pobranie i odszyfrowanie części paczki ZIP.

## Uwagi

Przepływ został przetestowany end-to-end na środowisku **demo**
(2026-08-26) — potwierdzone:

- prefix ścieżki API: `/api/v2`,
- pola JSON w `camelCase` (`subjectType`, `subject1`/`subject2`, `ksefNumber`),
- `dateType` przyjmuje `Issue` / `Invoicing` / `PermanentStorage`
  (metadane akceptują też małe litery; eksport wymaga `Subject1`/`Subject2`),
- challenge zwraca gotowe `timestampMs` (używane do szyfrowania tokena),
- `token` do zaszyfrowania = **pełny** string wygenerowany w Aplikacji
  Podatnika (format `numer|nip-XXX|sekret`), a nie sama jego część,
- eksport paczki: klucz AES-256 szyfrowany certyfikatem `SymmetricKeyEncryption`,
  paczka to ZIP zaszyfrowany **AES-256-CBC** (pliki części `*.zip.aes`).

**Limit zakresu dat:** pojedyncze zapytanie o metadane nie może przekraczać
**3 miesięcy** (błąd `21405`). Skrypt automatycznie dzieli dowolny zakres na
okna < 3 mies. (`date_windows`), więc `KSEF_DATE_FROM`/`KSEF_DATE_TO` mogą być
dowolnie szerokie.

Swagger poszczególnych środowisk:

- prod: <https://api.ksef.mf.gov.pl/docs/v2>
- demo: <https://api-demo.ksef.mf.gov.pl/docs/v2>
- test: <https://api-test.ksef.mf.gov.pl/docs/v2>

Do **masowego** pobierania dużych wolumenów użyj trybu `KSEF_MODE=export`
(asynchroniczna, zaszyfrowana paczka). Dla przyrostowego pobierania spójnych
danych warto filtrować po `KSEF_DATE_TYPE=PermanentStorage`.

Źródło flow: oficjalna dokumentacja MF – <https://github.com/CIRFMF/ksef-docs>
