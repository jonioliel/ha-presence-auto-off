"""Tests for integration setup and unload orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from custom_components.presence_auto_off import PLATFORMS, async_unload_entry


async def test_rejected_platform_unload_resumes_after_stopping_controller() -> None:
    """A rejected platform unload resumes only after automation was stopped."""
    call_order: list[str] = []
    controller = Mock()
    controller.async_stop = AsyncMock(
        side_effect=lambda: call_order.append("controller_stop")
    )
    controller.async_resume = AsyncMock(
        side_effect=lambda: call_order.append("controller_resume")
    )
    controller.async_unload = AsyncMock(
        side_effect=lambda: call_order.append("controller_unload")
    )
    entry = Mock(runtime_data=controller)
    config_entries = Mock()
    config_entries.async_unload_platforms = AsyncMock(
        side_effect=lambda *_args: call_order.append("platform_unload") or False
    )
    hass = Mock(config_entries=config_entries)

    result = await async_unload_entry(hass, entry)

    assert result is False
    assert call_order == [
        "controller_stop",
        "platform_unload",
        "controller_resume",
    ]
    controller.async_stop.assert_awaited_once_with()
    config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)
    controller.async_resume.assert_awaited_once_with()
    controller.async_unload.assert_not_awaited()
