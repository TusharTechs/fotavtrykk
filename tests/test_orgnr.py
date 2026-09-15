from fotavtrykk import orgnr


class TestValidation:
    def test_known_real_numbers_validate(self):
        # Equinor ASA and Brønnøysundregistrene itself.
        assert orgnr.is_valid("923609016")
        assert orgnr.is_valid("974760673")

    def test_spacing_variants_normalise(self):
        assert orgnr.normalise("923 609 016") == "923609016"
        assert orgnr.normalise("NO 923.609.016 MVA") == "923609016"
        assert orgnr.is_valid("923 609 016")

    def test_checksum_rejects_wrong_check_digit(self):
        assert not orgnr.is_valid("923609017")

    def test_rejects_wrong_length(self):
        assert not orgnr.is_valid("12345678")
        assert not orgnr.is_valid("")


class TestProof:
    ORG = "923609016"

    def test_labelled_org_number_is_strongest_proof(self):
        text = "Kontakt oss. Org.nr: 923 609 016. Alle rettigheter."
        proof, span = orgnr.find_org_number_proof(text, self.ORG)
        assert proof == orgnr.PROOF_KEYWORD
        assert "923 609 016" in span

    def test_norwegian_label_variants(self):
        for text in (
            "Organisasjonsnummer 923609016",
            "orgnr. 923.609.016",
            "MVA-nummer NO 923 609 016 MVA",
        ):
            proof, _ = orgnr.find_org_number_proof(text, self.ORG)
            assert proof == orgnr.PROOF_KEYWORD, text

    def test_bare_number_accepted_when_checksum_valid(self):
        proof, span = orgnr.find_org_number_proof("Foretaket 923 609 016 i Stavanger", self.ORG)
        assert proof in (orgnr.PROOF_KEYWORD, orgnr.PROOF_BARE)
        assert span

    def test_different_company_number_is_not_proof(self):
        proof, span = orgnr.find_org_number_proof("Org.nr 974 760 673", self.ORG)
        assert proof == orgnr.PROOF_NONE
        assert span is None

    def test_concatenated_digits_do_not_manufacture_a_match(self):
        """The digit-strip trick would match here. We must not."""
        text = "Ring 92 36 09 til avdeling 016 for mer informasjon."
        assert orgnr.find_org_number_proof(text, self.ORG)[0] == orgnr.PROOF_NONE

    def test_non_breaking_spaces_are_handled(self):
        proof, _ = orgnr.find_org_number_proof("Org.nr 923 609 016", self.ORG)
        assert proof == orgnr.PROOF_KEYWORD

    def test_empty_inputs_abstain(self):
        assert orgnr.find_org_number_proof("", self.ORG)[0] == orgnr.PROOF_NONE
        assert orgnr.find_org_number_proof("Org.nr 923 609 016", "")[0] == orgnr.PROOF_NONE


class TestNameTokens:
    def test_strips_legal_suffixes_and_nordic_characters(self):
        assert orgnr.name_tokens("Bærum Sykkelverksted AS") == ["baerum", "sykkelverksted"]

    def test_drops_generic_connectors(self):
        assert "og" not in orgnr.name_tokens("Hansen og Sønner AS")


class TestNegativeEvidence:
    """A page naming a different legal entity is not ours, however well the
    name matches. This is how parent and franchise sites capture subsidiaries."""

    ORG = "923609016"

    def test_a_different_valid_org_number_is_a_conflict(self):
        found = orgnr.find_conflicting_org_numbers("Org.nr 974 760 673", self.ORG)
        assert found == ["974760673"]

    def test_our_own_number_is_not_a_conflict(self):
        assert orgnr.find_conflicting_org_numbers("Org.nr 923 609 016", self.ORG) == []

    def test_invalid_checksums_are_not_conflicts(self):
        assert orgnr.find_conflicting_org_numbers("Ordrenummer 123 456 789", self.ORG) == []

    def test_a_page_with_no_numbers_has_no_conflict(self):
        assert orgnr.find_conflicting_org_numbers("Velkommen til oss", self.ORG) == []

    def test_multiple_conflicts_are_all_returned(self):
        found = orgnr.find_conflicting_org_numbers(
            "Org.nr 974 760 673 og 810 034 882", self.ORG
        )
        assert found == ["810034882", "974760673"]
