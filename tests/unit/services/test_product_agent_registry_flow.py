"""Exercise the complete publication lifecycle through HTTP commands."""

from tests.unit.services.test_product_agent_registry import command, publish
from tests.unit.services.test_product_agent_registry import registry as registry
from tests.unit.services.test_product_agent_registry_api import BASE, HEADERS
from tests.unit.services.test_product_agent_registry_api import client as client


def test_publication_lifecycle_through_http(client):
    def send(request):
        response = client.post(BASE + "/commands", headers=HEADERS, json=request.model_dump())
        assert response.status_code == 200, response.text
        return response.json()

    send(command("RegisterPrincipal", display_name="Assistant"))
    send(publish())
    assert (
        client.get(BASE + "/agents.example/assistant/resolve", headers=HEADERS).status_code == 404
    )
    send(
        command(
            "DecideVersion", version="v1", decision="approved", reason="reviewed", expires_at=2000
        )
    )
    send(
        command(
            "MoveChannel", channel="stable", version="v1", expected_revision=0, reason="publish"
        )
    )
    selected = client.get(BASE + "/agents.example/assistant/resolve", headers=HEADERS)
    assert selected.status_code == 200 and selected.json()["version"]["manifest"]["version"] == "v1"
    send(command("DecideVersion", version="v1", decision="revoked", reason="withdraw access"))
    assert (
        client.get(BASE + "/agents.example/assistant/resolve", headers=HEADERS).status_code == 404
    )
    history = client.get(BASE + "/agents.example/assistant/versions/v1", headers=HEADERS)
    assert history.status_code == 200 and history.json()["decision"]["decision"] == "revoked"
