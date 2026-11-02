"""Compatibility checks for inherited areas and rule control targets."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from custom_components.presence_auto_off.helpers import effective_area_id


@pytest.mark.parametrize(
    ("entity_area", "child_area", "parent_area", "expected"),
    [
        ("entity_room", "child_room", "parent_room", "entity_room"),
        (None, "child_room", "parent_room", "child_room"),
        (None, None, "parent_room", "parent_room"),
        (None, None, None, None),
    ],
)
def test_child_device_area_inheritance(
    entity_area: str | None,
    child_area: str | None,
    parent_area: str | None,
    expected: str | None,
) -> None:
    """Entity and child overrides precede the parent's inherited area."""
    devices = {
        "child": SimpleNamespace(area_id=child_area, parent_device_id="parent"),
        "parent": SimpleNamespace(area_id=parent_area),
    }
    registry = Mock()
    registry.async_get.side_effect = devices.get
    entity = SimpleNamespace(area_id=entity_area, device_id="child")
    with patch(
        "custom_components.presence_auto_off.helpers.dr.async_get",
        return_value=registry,
    ):
        assert effective_area_id(Mock(), entity) == expected


def test_legacy_device_without_parent_is_supported() -> None:
    """HA releases before child devices continue to fail closed without an area."""
    registry = Mock()
    registry.async_get.return_value = SimpleNamespace(area_id=None)
    with patch(
        "custom_components.presence_auto_off.helpers.dr.async_get",
        return_value=registry,
    ):
        assert (
            effective_area_id(Mock(), SimpleNamespace(area_id=None, device_id="legacy"))
            is None
        )
