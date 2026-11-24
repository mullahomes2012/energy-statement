import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.failed:
        import sys
        sys.__stderr__.write("\n=== FAILURE " + item.name + " ===\n" + str(rep.longreprtext)[-6000:] + "\n")
        sys.__stderr__.flush()


@pytest.fixture(autouse=True)
async def unload_entries_at_end(hass):
    """Unload entries so no recorder query is in flight when the test DB closes."""
    yield
    for entry in hass.config_entries.async_entries("energy_statement"):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
