"""Curio serves its API and preservation skills."""


def test_api_skill_served(http_client):
    response = http_client.get("/skill")
    assert response.status_code == 200
    assert "name: curio" in response.text
    # the canonical-path alias still works
    assert http_client.get("/skill/SKILL.md").text == response.text


def test_shipped_preservation_skill_served(http_client):
    response = http_client.get("/skill/nft-preservation")
    assert response.status_code == 200
    assert "name: nft-preservation" in response.text
    assert "# NFT media with Curio" in response.text
    # full path spelling too
    assert http_client.get("/skill/nft-preservation/SKILL.md").text == response.text


def test_unknown_skill_404s_with_the_available_list(http_client):
    response = http_client.get("/skill/definitely-not-a-skill")
    assert response.status_code == 404
    assert "nft-preservation/SKILL.md" in response.json()["available"]
