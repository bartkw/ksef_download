# Pobieranie faktur z KSeF 2.0

Skrypt w Pythonie pobierający faktury (sprzedażowe i zakupowe) z Krajowego
Systemu e-Faktur (KSeF 2.0) przy użyciu **tokena KSeF**. Obsługuje wiele firm,
zapis XML lub PDF (oficjalna wizualizacja MF), podział na miesiące, rejestr CSV
i pracę przyrostową.

## Spis treści

- [Wymagania](#wymagania)
- [Instalacja](#instalacja)
- [Konfiguracja](#konfiguracja)
- [Uruchomienie](#uruchomienie)
- [Format zapisu (XML / PDF)](#format-zapisu-xml--pdf)
- [Tryby pobierania (single / export)](#tryby-pobierania-single--export)
- [Zestawienia i wygoda](#zestawienia-i-wygoda)
- [Automatyzacja (cron)](#automatyzacja-cron)
- [Log i weryfikacja integralności](#log-i-weryfikacja-integralności)
- [Jak to działa (KSeF 2.0)](#jak-to-działa-ksef-20)
- [Uwagi](#uwagi)

## Wymagania

- Python 3.10+
- Token KSeF wygenerowany w Aplikacji Podatnika KSeF (w kontekście Twojej firmy)
- Dla `KSEF_FORMAT=pdf`: Node.js ≥ 20 + generator MF (patrz „Format zapisu")

## Instalacja

**Szybko (macOS / Linux)** — jednym poleceniem:

```bash
bash setup.sh
```

`setup.sh` wykonuje kolejno:

1. sprawdza `python3` (3.10+),
2. instaluje zależności Pythona (`requests`, `cryptography`, `pypdf`) — z
   fallbackami dla środowisk „externally-managed" (`--user` /
   `--break-system-packages`, a w ostateczności podpowiada `venv`),
3. wykrywa Node.js (Homebrew Apple Silicon `/opt/homebrew`, Intel `/usr/local`,
   `PATH`, nvm) — wymagany tylko dla `KSEF_FORMAT=pdf`,
4. klonuje generator PDF `CIRFMF/ksef-pdf-generator` (jeśli go brak)
   i uruchamia `npm install` **na tej maszynie**,
5. tworzy `.env` z `env.example.txt` (istniejącego nie nadpisuje),
6. robi self-check (moduł się ładuje, Node wykryty, generator gotowy).

Po zakończeniu uzupełnij `.env` (i/lub `firmy.json`) i uruchom skrypt.
Bez Node.js tryb `pdf` nie zadziała, ale tryb `xml` owszem.

**Ręcznie:**

```bash
pip install -r requirements.txt
# dla KSEF_FORMAT=pdf dodatkowo: Node.js + generator (patrz „Format zapisu")
```

## Konfiguracja

Ustawienia dzielą się na **firmy** (tokeny + NIP-y) i **opcje wspólne** (`.env`).

### Firmy (`firmy.json`) — obsługa wielu firm

Skopiuj `firmy.example.json` do `firmy.json` i wpisz swoje firmy:

```json
{
  "firmy": [
    { "nazwa": "Moja Firma",       "nip": "8792408754", "token": "…", "env": "prod" },
    { "nazwa": "Druga Sp. z o.o.", "nip": "5260250995", "token": "…", "env": "prod" },
    { "nazwa": "Testowa (demo)",   "nip": "1111111111", "token": "…", "env": "demo" }
  ]
}
```

Przy uruchomieniu skrypt **zapyta, dla której firmy** pobrać faktury. Każda firma
ma własny folder wyników: `faktury/<nazwa firmy>/…`.

> Jeśli nie utworzysz `firmy.json`, skrypt użyje pojedynczej firmy z `.env`
> (`KSEF_TOKEN` + `KSEF_NIP`) — działa jak wcześniej.

### Opcje wspólne (`.env`)

Skopiuj `env.example.txt` do `.env` i ustaw:

- `KSEF_DATE_TYPE` – typ daty filtrowania: `Issue` (wystawienia, domyślnie),
  `Invoicing` (przyjęcia w KSeF) lub `PermanentStorage` (trwałego zapisu),
- `KSEF_MODE` – tryb pobierania: `single` (domyślnie) lub `export`,
- `KSEF_FORMAT` – format zapisu: `xml` (domyślnie) lub `pdf`,
- `KSEF_DATE_FROM`, `KSEF_DATE_TO` – zakres dat dla uruchomień
  **nieinteraktywnych** (np. cron); interaktywnie skrypt pyta o daty.

> ⚠️ Nie commituj `firmy.json` ani `.env` (zawierają tokeny). Oba są w `.gitignore`.

## Uruchomienie

```bash
python ksef_pobierz.py
```

Skrypt zapyta o **firmę** i **zakres dat**, po czym pobierze faktury do folderu
danej firmy (w podziale na sprzedaż/zakup i podfoldery **rok-miesiąc**).

### Wybór firmy i zakresu dat

- **Firma** — menu z listą firm z `firmy.json`; opcja `0) Wszystkie firmy`
  przetwarza po kolei każdą firmę.
- **Zakres dat**:
  1. **Nowe z bieżącego miesiąca** (domyślne) — istniejące pomijane,
  2. **Od ostatniego pobrania** — od daty poprzedniego uruchomienia dla danej
     firmy (zapamiętanej w `.stan.json`), do dziś,
  3. **Ręcznie** — własny zakres `RRRR-MM-DD`.

Struktura wyników:

```
faktury/
└── Moja_Firma/                 # osobny folder na każdą firmę
    ├── sprzedaz/               # faktury wystawione przez tę firmę (subject1)
    │   ├── 2026-07/            # faktury z lipca 2026
    │   └── 2026-08/
    ├── zakup/                  # faktury wystawione na tę firmę (subject2)
    │   └── 2026-08/
    ├── rejestr_2026-08.csv     # zestawienie faktur miesiąca (dla księgowości)
    └── _do_druku/              # scalone PDF-y miesiąca (tylko KSEF_FORMAT=pdf)
        ├── sprzedaz_2026-08.pdf
        └── zakup_2026-08.pdf
```

Miesiąc ustalany jest według wybranego `KSEF_DATE_TYPE` (domyślnie data
wystawienia). Skrypt pomija faktury już zapisane na dysku (po numerze KSeF),
więc można go uruchamiać wielokrotnie – działa przyrostowo.

### Argumenty wiersza poleceń (opcjonalne)

```
python ksef_pobierz.py [-q|--quiet] [--firma X] [--od RRRR-MM-DD] [--do RRRR-MM-DD] [--od-ostatniego]
```

- `--quiet` — bez wypisywania na konsolę (nadal loguje do pliku); tryb nieinteraktywny,
- `--firma` — numer, nazwa lub `all` (pomija menu),
- `--od` / `--do` — zakres dat bez pytania,
- `--od-ostatniego` — pobierz od ostatniego pobrania.

## Format zapisu (XML / PDF)

Ustawiany przez `KSEF_FORMAT`:

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
> i uruchom `npm install` jeszcze raz (jest w `.gitignore`).

**macOS:** zainstaluj Node przez Homebrew (`brew install node`) — skrypt sam go
znajdzie (Apple Silicon `/opt/homebrew`, Intel `/usr/local`). Jeśli `node` nie
jest w `PATH`, wskaż go zmienną `KSEF_NODE=/ścieżka/do/node`. Najprościej:
`bash setup.sh` zrobi to wszystko.

## Tryby pobierania (single / export)

Ustawiane przez `KSEF_MODE`:

- **`single`** (domyślny) – dla każdej faktury osobne `GET /invoices/ksef/{num}`.
  Prosty i wystarczający dla typowej firmy.
- **`export`** – asynchroniczny eksport **zaszyfrowanej paczki ZIP**
  (`POST /invoices/exports`). Zalecany do **masowego** pobierania dużych
  wolumenów. Skrypt:
  1. generuje klucz AES-256 + IV, szyfruje klucz kluczem publicznym MF
     (RSA-OAEP SHA-256, certyfikat `SymmetricKeyEncryption`),
  2. zleca eksport i odpytuje status aż do zakończenia (`200`),
  3. pobiera części paczki (bez tokenu – osobny storage), weryfikuje ich skróty
     SHA-256, odszyfrowuje (AES-256-CBC) i zapisuje faktury (XML lub PDF)
     do folderu firmy, tak samo jak tryb `single`.

  Uwaga: jedna paczka mieści do **10 000 faktur**; przy przekroczeniu limitu
  (`isTruncated`) skrypt to zgłasza – zawęź wtedy zakres dat.

## Zestawienia i wygoda

Po pobraniu skrypt dodatkowo:

- **Rejestr CSV** (`rejestr_RRRR-MM.csv` per firma/miesiąc) — numer KSeF, numer
  faktury, kontrahent, daty, netto/VAT/brutto, waluta. Format PL (średnik,
  przecinek dziesiętny, `utf-8-sig`) — otwiera się wprost w Excelu. Kolejne
  uruchomienia **dokładają** wpisy (scalanie po numerze KSeF).
- **Scalony PDF miesiąca** (`_do_druku/<typ>_RRRR-MM.pdf`) — wszystkie faktury
  danego miesiąca w jednym pliku, wygodne do druku jednym zleceniem
  (tylko przy `KSEF_FORMAT=pdf`; wymaga `pypdf`).
- **Podsumowanie kwot** na koniec — sumy netto/VAT/brutto osobno dla sprzedaży
  i zakupu.

## Automatyzacja (cron)

Do uruchomień bez pytań (np. w cronie) użyj argumentów lub zmiennych:

- `--firma` / `KSEF_FIRMA` – numer, nazwa firmy z `firmy.json` albo `all`,
- `--od`/`--do` / `KSEF_DATE_FROM`/`KSEF_DATE_TO` – zakres dat,
- `--od-ostatniego` / `KSEF_SINCE_LAST=1` – od ostatniego pobrania,
- `--quiet` – cicho, tylko log do pliku.

Przykład (wszystkie firmy, przyrostowo, cicho):

```bash
python3 ksef_pobierz.py --firma all --od-ostatniego --quiet
```

## Log i weryfikacja integralności

- **Log** — każdy przebieg jest dopisywany do `logs/ksef.log` (z datą i godziną),
  przydatne pod crona i do diagnostyki.
- **Weryfikacja integralności** — po pobraniu skrypt porównuje `invoiceHash`
  z metadanych z SHA-256 pobranego XML; przy niezgodności wypisuje ostrzeżenie
  (nie przerywa pracy).

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

Przepływ został przetestowany end-to-end na środowisku **demo** — potwierdzone:

- prefix ścieżki API: `/api/v2`,
- pola JSON w `camelCase` (`subjectType`, `subject1`/`subject2`, `ksefNumber`),
- `dateType` przyjmuje `Issue` / `Invoicing` / `PermanentStorage`
  (metadane akceptują też małe litery; eksport wymaga `Subject1`/`Subject2`),
- challenge zwraca gotowe `timestampMs` (używane do szyfrowania tokena),
- `token` do zaszyfrowania = **pełny** string wygenerowany w Aplikacji
  Podatnika (format `numer|nip-XXX|sekret`), a nie sama jego część,
- eksport paczki: klucz AES-256 szyfrowany certyfikatem `SymmetricKeyEncryption`,
  paczka to ZIP zaszyfrowany **AES-256-CBC** (pliki części `*.zip.aes`),
- `invoiceHash` z metadanych = `base64(SHA-256(XML))` (podstawa weryfikacji).

**Limit zakresu dat:** pojedyncze zapytanie o metadane nie może przekraczać
**3 miesięcy** (błąd `21405`). Skrypt automatycznie dzieli dowolny zakres na
okna < 3 mies. (`date_windows`), więc zakres dat może być dowolnie szeroki.

**Przyrostowe pobieranie spójnych danych:** warto filtrować po
`KSEF_DATE_TYPE=PermanentStorage` (dane poniżej tego znacznika już się nie
zmieniają).

Swagger poszczególnych środowisk:

- prod: <https://api.ksef.mf.gov.pl/docs/v2>
- demo: <https://api-demo.ksef.mf.gov.pl/docs/v2>
- test: <https://api-test.ksef.mf.gov.pl/docs/v2>

Źródło flow: oficjalna dokumentacja MF – <https://github.com/CIRFMF/ksef-docs>
