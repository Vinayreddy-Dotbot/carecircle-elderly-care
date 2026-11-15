"""Issues 18-22: the checker normalises names, covers drug classes, and says what it could not check."""
import interactions as ix


def test_brand_names_map_to_generics():
    assert ix.ingredients("Ecosprin 75") == {"aspirin"}
    assert ix.ingredients("Glycomet SR 500") == {"metformin"}
    assert ix.ingredients("Thyronorm 50mcg") == {"levothyroxine"}
    assert ix.ingredients("Combiflam") == {"ibuprofen", "paracetamol"}
    assert ix.ingredients("Metformin500") == {"metformin"}
    assert ix.ingredients("Frusemide") == {"furosemide"}


def test_no_substring_false_positives():
    assert ix.ingredients("Environ cream") == set()
    assert ix.ingredients("Ironwood") == set()


def test_original_pairs_still_warn():
    assert ix.check("Ibuprofen", ["Warfarin"]).warnings
    assert ix.check("Sildenafil", ["Nitroglycerin"]).warnings
    assert ix.check("Levothyroxine", ["Calcium + Vitamin D3"]).warnings


def test_brand_and_class_matches_warn():
    assert "bleeding" in ix.check("Ecosprin", ["Coumadin"]).warnings[0]
    assert "bleeding" in ix.check("Naproxen", ["Warfarin"]).warnings[0]         # NSAID class
    assert ix.check("Ramipril", ["Aldactone"]).warnings                          # ACE inhibitor class
    assert ix.check("Cialis", ["Sorbitrate"]).warnings                           # PDE5 + nitrate


def test_one_message_per_rule_group():
    w = ix.check("Ibuprofen", ["Aspirin"]).warnings
    assert len(w) == 1 and "heart-protective" in w[0]


def test_hidden_duplicates_in_combination_products():
    w = ix.check("Crocin", ["Combiflam"]).warnings
    assert any("both contain paracetamol" in x for x in w)
    assert any("double dosing" in x for x in ix.check("Glycomet", ["Metformin"]).warnings)
    assert any("NSAID" in x for x in ix.check("Diclofenac", ["Ibuprofen"]).warnings)


def test_unrecognised_names_are_reported_with_suggestions():
    r = ix.check("Metfromin", ["Aspirin"])
    assert r.unrecognised == ["Metfromin"] and r.suggestions["Metfromin"] == "metformin"
    r = ix.check_all(["Vitamin D", "Aspirin"])
    assert r.unrecognised == ["Vitamin D"] and not r.warnings


def test_coverage_is_reported_honestly():
    c = ix.coverage()
    assert c["ingredients"] >= 30 and c["reviewed_by"] is None
    assert "does not mean" in ix.DISCLAIMER
