"""Tests for An Post diagnostics."""
from unittest.mock import MagicMock

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from custom_components.an_post.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.data = {CONF_EMAIL: "user@example.test", CONF_PASSWORD: "hunter2"}
    entry.options = {}
    entry.runtime_data.account = {"uid": "uid-123", "email": "user@example.test"}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "AB123456789IE",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "in_transit",
            "raw": {
                "trackingNumber": "AB123456789IE",
                "recipientName": "Jane Doe",
                "recipientAddress": "1 Example Street, Dublin",
                "deliveryPin": "1234",
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    # credentials, tracking codes and payload PII are redacted, at every level
    assert result["entry_data"][CONF_EMAIL] == "**REDACTED**"
    assert result["entry_data"][CONF_PASSWORD] == "**REDACTED**"
    assert result["account"]["uid"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["recipientName"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["recipientAddress"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["deliveryPin"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "in_transit"
