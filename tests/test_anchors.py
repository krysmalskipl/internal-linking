from internal_linking.anchors import AnchorFinder, content_words, same_word


def test_same_word_matches_inflection_only():
    assert same_word("konsultacja", "konsultacji")
    assert same_word("wymiana", "wymianie")
    assert same_word("wizyta", "wizytach")
    assert not same_word("kontakt", "kontrolom")
    assert not same_word("realizacje", "realne")
    assert not same_word("mikroiglowa", "mikrobiomu")


def test_content_words_drop_fillers_and_numbers():
    assert content_words("Kompletny poradnik: audyt SEO w 2026 roku") == ["audyt", "seo"]


def test_exact_match_in_inflected_form(site):
    finder = AnchorFinder(site)
    found = finder.find("Po wymianie opon zimowych warto sprawdzić koła.", "Ustawienie geometrii kół",
                        "Wymiana opon zimowych", "Wymiana opon zimowych", "https://example.pl/wymiana-opon/")
    assert found == ("wymianie opon zimowych", "exact")


def test_single_word_is_never_an_anchor(site):
    finder = AnchorFinder(site)
    assert finder.find("Masz pytania? Kontakt przez formularz.", "Remonty mieszkań",
                       "Kontakt", "Kontakt", "https://example.pl/kontakt/") is None


def test_phrase_about_the_source_page_is_skipped(site):
    finder = AnchorFinder(site)
    # the phrase names the source page's own topic - linking it away would cannibalise
    assert finder.find("Serwis klimatyzacji samochodowej robimy od ręki.", "Serwis klimatyzacji samochodowej",
                       "Klimatyzacja samochodowa serwis", "", "https://example.pl/inna/") is None


def test_phrase_never_spans_an_existing_link(site):
    finder = AnchorFinder(site)
    # "wymianę" sits before an existing link (¶) - the phrase may only use the text after it
    found = finder.find("Oferujemy wymianę ¶ opon zimowych", "Remonty mieszkań", "Wymiana opon zimowych",
                        "", "https://example.pl/wymiana-opon/")
    assert found == ("opon zimowych", "partial")


def test_exact_phrase_has_no_extra_words(site):
    finder = AnchorFinder(site)
    found = finder.find("Sprawdź to w Google Search Console używając raportu.", "Remonty mieszkań",
                        "Google Search Console (GSC) - jak używać", "Google Search Console",
                        "https://example.pl/google-search-console/")
    assert found == ("Google Search Console", "exact")


def test_headings_are_never_used(site):
    finder = AnchorFinder(site)
    blocks = [{"tag": "h2", "text": "Wymiana opon zimowych"},
              {"tag": "p", "text": "Zadbaj o wymianę opon zimowych przed sezonem."}]
    phrase, kind, index = finder.find_in_blocks(blocks, "Remonty mieszkań", "Wymiana opon zimowych", "",
                                                "https://example.pl/wymiana-opon/")
    assert (phrase, kind, index) == ("wymianę opon zimowych", "exact", 1)


def test_english_inflection_and_fillers():
    pages = [{"url": f"https://example.com/{i}/", "title": t, "h1": t, "lang": "en",
              "text": "We renovate flats and houses across the city. " * 30}
             for i, t in enumerate(["Home", "Bathroom renovation", "Kitchen renovation", "Contact"])]
    finder = AnchorFinder(pages)
    found = finder.find("We finished two bathroom renovations this month.", "Home",
                        "Bathroom renovation", "", "https://example.com/bathroom-renovation/", "en")
    assert found == ("bathroom renovations", "exact")
    # "the ultimate guide" is filler, never a keyword
    assert content_words("The ultimate guide to bathroom renovation", "en") == ["bathroom", "renovation"]


def test_language_is_detected_from_text_when_html_lang_is_missing():
    from internal_linking.anchors import detect_lang
    assert detect_lang({"lang": "", "text": "The best way to plan the renovation of your bathroom is to start "
                                            "with the layout and the budget for tiles and fittings."}) == "en"
    assert detect_lang({"lang": "", "text": "Remont łazienki warto zacząć od projektu i budżetu na płytki, "
                                            "armaturę oraz robociznę, a potem wybrać wykonawcę."}) == "pl"
    assert detect_lang({"lang": "de", "text": "Die Renovierung des Badezimmers beginnt mit der Planung."}) == ""


# phrases cut wrong on a labelled run of a real blog - the good link was there, the phrase was not
def test_a_phrase_stops_at_a_bracket(site):
    finder = AnchorFinder(site)
    found = finder.find("Poprawne indeksowanie strony (czy Google widzi podstrony) sprawdzisz w raporcie.",
                        "Google Search Console", "Indeksowanie strony w Google", "Indeksowanie strony w Google",
                        "https://example.pl/indeksowanie-strony-w-google/")
    assert found == ("indeksowanie strony", "partial")
    assert finder.find("To jest SEO (Search Engine Optimization) w praktyce.", "Strona o czymś",
                       "Nagłówki H1 w SEO i Search", "", "https://example.pl/naglowki/") is None


def test_a_partial_phrase_never_spans_a_conjunction(site):
    finder = AnchorFinder(site)
    assert finder.find("Monitoruj zapytania w narzędziach takich jak ChatGPT czy Gemini regularnie.", "AEO kontra SEO",
                       "Pozycjonowanie w AI: ChatGPT i Gemini dla firm", "", "https://example.pl/ai-lokalne/") is None
    # a full page name may contain one
    found = finder.find("Robimy też remonty i wykończenia wnętrz od podstaw.", "Strona o firmie",
                        "Remonty i wykończenia", "", "https://example.pl/remonty-i-wykonczenia/")
    assert found == ("remonty i wykończenia", "exact")


def test_a_partial_phrase_ends_on_the_targets_own_word_not_a_shared_stem(site):
    finder = AnchorFinder(site)
    found = finder.find("Plik robots.txt blokuje jedynie wejście robota na stronę.", "Indeksowanie",
                        "Co to jest plik robots.txt? Poradnik sprawdzania blokad", "",
                        "https://example.pl/robots-txt/")
    assert found == ("Plik robots.txt", "partial")
    assert finder.find("Wizyta zaczyna się od analizy skóry.", "Stymulatory tkankowe",
                       "Zacznij od początku - pierwsza wizyta", "", "https://example.pl/pierwsza-wizyta/") is None


def test_text_outside_blocks_is_never_a_link_source(site):
    finder = AnchorFinder(site)
    blocks = [{"tag": "inne", "text": "Opinie: Anna K Wymiana opon zimowych Kasia W Rekonsultacja"}]
    assert finder.find_in_blocks(blocks, "Realizacje", "Wymiana opon zimowych", "Wymiana opon zimowych",
                                 "https://example.pl/wymiana-opon/") is None
