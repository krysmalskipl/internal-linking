# internal-linking

Rekomendacje linków wewnętrznych z modelem Jev (TypeSafe) przez OpenRouter. Jev nie pisze tekstu - dla każdej strony wybiera z listy kandydatów najlepszy cel linku i zwraca prawdopodobieństwo. Próg akceptacji kalibrujesz na ręcznie ocenionej próbce.

## instalacja

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # i wpisz klucz OpenRouter w .env
```

## przepływ

Wszystkie pliki trafiają do `data/<domena>/`.

1. **crawl** - sitemapa (z `robots.txt` lub standardowych adresów) → `pages.jsonl` z tytułem, h1, meta, treścią główną i linkami: w treści, w okruszkach i poza treścią (menu, stopka, powiązane wpisy). Pomija nie-200, przekierowania, noindex i canonical na inny URL.
   ```bash
   .venv/bin/python crawl.py --domain example.com
   ```
2. **recommend** - dla każdej strony źródłowej (≥ 150 słów) tylko cele, dla których w jej treści jest fraza na anchor (exact albo partial, `anchors.py`); do 254 kandydatów (bez stron, do których źródło już linkuje w treści, okruszkach lub blokach tylko tej strony - cele z menu i stopki zostają, bo link w treści ma inną wartość; przy większych serwisach wybór wg TF-IDF + ten sam folder URL) i jedno zapytanie `choice` do Jev z opcją `brak`. Odpowiedzi są w cache `jev_raw.jsonl` - ponowne uruchomienie nic nie kosztuje. Wynik: `recommendations.csv` z top 3 celami na stronę.
   ```bash
   .venv/bin/python recommend.py --domain example.com --dry-run   # szacunek tokenów, bez API
   .venv/bin/python recommend.py --domain example.com --limit 3   # test na 3 stronach
   .venv/bin/python recommend.py --domain example.com
   ```
3. **sample** - 40 par równo z 5 kwantyli prawdopodobieństwa → `sample_to_label.csv`. Oceń je w przeglądarce (`label.py`, pytania Tak / Nie, zapis od razu do CSV) albo wpisz w kolumnie `ok` 1 (dobry link) lub 0 (zły).
   ```bash
   .venv/bin/python sample.py --domain example.com
   .venv/bin/python label.py --domain example.com   # http://localhost:8765
   ```
4. **calibrate** - tabela precyzja/pokrycie dla progów, wybór najniższego progu z precyzją ≥ docelowej (i co najmniej 5 parami powyżej) → `final.csv` z kolumną `decision`: `accept (ręcznie)`, `auto_accept`, `review`, `reject (ręcznie)`.
   ```bash
   .venv/bin/python calibrate.py --domain example.com --target-precision 0.9
   ```

## kolumny `recommendations.csv`

- `probability` - prawdopodobieństwo Jev dla tego celu (skala zależy od liczby kandydatów, dlatego próg wynika z kalibracji, a nie z góry)
- `p_none` - prawdopodobieństwo opcji "brak dobrego celu" dla strony źródłowej
- `jev_choice` - `brak`, jeśli Jev uznał, że żaden cel nie pasuje
- `in_menu` - 1, jeśli cel jest już w menu lub stopce całego serwisu (link w treści nadal ma sens, ale to mniejszy zysk niż dla strony bez żadnego linku)
- `anchor` - fraza z treści źródła, na którą trafia link (każda rekomendacja ją ma)
- `anchor_type` - `exact` (fraza pokrywa całą nazwę celu) albo `partial` (co najmniej 2 słowa z nazwy celu)

Klient API w `jev_client.py` bazuje na `~/.claude/skills/jev/jev.py` (model `typesafe/jev-1.13`, zmienne `JEV_MODEL` / `JEV_API_URL` nadpisują domyślne).
