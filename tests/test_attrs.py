from app.normalize.attrs import canonical_body, canonical_color


def test_colors_across_languages() -> None:
    assert canonical_color("Schwarz Metallic") == "black"
    assert canonical_color("Grijs") == "grey"
    assert canonical_color("Argento") == "silver"
    assert canonical_color("Blu") == "blue"
    assert canonical_color("Saphirschwarz") == "black"
    assert canonical_color("Mint") == "other"
    assert canonical_color(None) is None


def test_bodies_with_and_without_model_hint() -> None:
    assert canonical_body("Variant") == "estate"
    assert canonical_body("T-Modell") == "estate"
    assert canonical_body("Hatchback 5-deurs") == "hatchback"
    assert canonical_body("Cabriolet") == "convertible"
    assert canonical_body("Coupé") == "coupe"
    assert canonical_body("Sportsvan") == "mpv"
    assert canonical_body("Standardmodell", []) == "hatchback"  # a Golf
    assert canonical_body("Standardmodell", ["sedan"]) == "sedan"  # a sedan-only model
    assert canonical_body("Standardmodell", ["sedan", "estate"]) is None  # unknown
    assert canonical_body("sedan") == "sedan" and canonical_body(None) is None
    assert canonical_body("Overige carrosserie") is None
