"""Diagnostics remain available when a rule has no active controller."""

from types import SimpleNamespace

import pytest

from custom_components.presence_auto_off.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.mark.parametrize("has_controller", [False, True])
async def test_diagnostics_without_setup(has_controller: bool) -> None:
    """A disabled or failed entry can still export its saved configuration."""
    entry = SimpleNamespace(
        entry_id="entry", title="Room", data={"rule_id": "room"}, options={}
    )
    if has_controller:
        entry.runtime_data = SimpleNamespace(diagnostics={"enabled": False})

    result = await async_get_config_entry_diagnostics(None, entry)

    assert result["config_entry"]["data"] == {"rule_id": "room"}
    assert result["runtime"] == ({"enabled": False} if has_controller else None)
