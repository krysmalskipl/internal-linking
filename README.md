# internal-linking

Propozycje linków wewnętrznych dla dowolnych domen, jedną komendą. Narzędzie szuka w treści stron fraz, które już istnieją (exact albo partial match), a każdą parę strona → fraza → cel ocenia model Jev (TypeSafe) przez OpenRouter. Wynik to raport HTML i CSV z linkami do wstawienia, niezależny od CMS.

## instalacja

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # i wpisz klucz OpenRouter w .env
```

## użycie

```bash
.venv/bin/python run.py --domain example.com
.venv/bin/python run.py --domain a.pl --domain b.pl        # kilka domen
.venv/bin/python run.py --domains-file domeny.txt          # jedna domena w wierszu
.venv/bin/python run.py --domain example.com --dry-run     # liczba par i koszt, bez Jev
.venv/bin/python run.py --domain example.com --no-crawl    # bez ponownego pobierania stron
.venv/bin/python run.py --domain example.com --max-links 5 # limit nowych linków na stronę (domyślnie 3)
```

Wyniki w `data/<domena>/`:
- `report.html` - strony z nowymi linkami: fragment tekstu z podświetloną frazą, cel, oceny; na dole odrzucone propozycje z powodami
- `links.csv` - linki do wstawienia
- `recommendations.csv` - wszystkie ocenione propozycje z decyzją i powodem

Odpowiedzi Jev są w cache (`jev_pairs.jsonl`), więc ponowne uruchomienie płaci tylko za nowe albo zmienione pary. Orientacyjnie: serwis na 50 stron to ok. minuta i ok. $0,01.

## jak to działa

1. **crawl** (`crawl.py`) - sitemapa z `robots.txt` albo standardowych adresów; dla każdej strony tytuł, H1, opis, język, bloki treści z typem (akapit, punkt listy, nagłówek...) i istniejące linki (w treści, w okruszkach, poza treścią). Pomija przekierowania, `noindex` i canonical na inny adres.
2. **frazy** (`anchors.py`) - w akapitach i listach (nigdy w nagłówkach) szuka fraz pasujących do tytułu, H1 albo sluga celu, z uwzględnieniem polskiej odmiany:
   - `exact` - fraza pokrywa całą nazwę celu,
   - `partial` - co najmniej 2 słowa z nazwy celu, w tym jedno charakterystyczne,
   - tekst istniejących linków i kodu nigdy nie jest anchorem; bez pojedynczych słów i ogólników.
3. **reguły** (`run.py`) - bez celów, do których strona już linkuje (menu i stopka się nie liczą), tylko w obrębie jednego języka, bez fraz powtarzanych jak szablon (np. podpis autora), fraza będąca dokładną nazwą innej strony jest zarezerwowana dla niej.
4. **Jev** (`jev_pairs.py`) - jedno wywołanie na parę, pytania zamknięte: czy fragment dotyczy tematu celu, czy fraza jest naturalnym anchorem, wartość linku (0-4), kanibalizacja, ogólna ocena z powodem. Podgląd pytań: `python jev_pairs.py --print-questions`.
5. **wybór** - progi z `config.json` (wspólne dla wszystkich domen), jedna fraza = jeden link, maks. N linków na stronę wg oceny.

## progi i kontrola jakości (opcjonalnie)

Progi w `config.json` są ustawione na ręcznie ocenionych próbkach. Żeby je sprawdzić albo poprawić:

```bash
.venv/bin/python sample.py --domain example.com       # losowa próbka propozycji
.venv/bin/python label.py --domain example.com        # ocena Tak / Nie w przeglądarce (localhost:8765)
.venv/bin/python evaluate.py --domain example.com --write-config   # AUC sygnałów i nowe progi
```

Pytania do Jev i lista słów pomijanych są po polsku - narzędzie jest przeznaczone dla polskich serwisów.

Klient API w `jev_client.py` (model `typesafe/jev-1.13`, zmienne `JEV_MODEL` / `JEV_API_URL` nadpisują domyślne).
