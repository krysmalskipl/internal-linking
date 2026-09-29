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
