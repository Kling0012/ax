from core.intake.dest.b12b3.subloop import decode


def test_decode_preserves_authority_and_copies_mutable_values():
    payload = {
        "plugin_id": "example", "project": "ax", "title": "Title", "body": "Body",
        "spec": {"secrets": [], "outputs": [], "sensitivity": "public"},
        "acceptance": {"examples": ["accept"], "boundaries": ["reject"]},
        "quote": "user request",
    }
    result = decode(payload)
    payload["spec"]["outputs"].append("publish")
    payload["acceptance"]["examples"].append("change")
    assert result.plugin_id == "example"
    assert result.spec == {"secrets": [], "outputs": [], "sensitivity": "public"}
    assert result.acceptance == {"examples": ["accept"], "boundaries": ["reject"]}
    assert result.quote == "user request"
