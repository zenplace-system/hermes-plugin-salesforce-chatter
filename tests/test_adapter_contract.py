import inspect

import pytest

ENV = {
    "SF_CHATTER_LOGIN_URL": "https://test.salesforce.com",
    "SF_CHATTER_CLIENT_ID": "cid",
    "SF_CHATTER_USERNAME": "bot@example.com",
    "SF_CHATTER_PRIVATE_KEY_PATH": "/tmp/k.key",
    "SF_CHATTER_BOT_USER_ID": "005000000000BOT",
}


class Ctx:
    def __init__(self):
        self.entries = []

    def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None, required_env=None,
                          install_hint="", **entry_kwargs):
        from gateway.platform_registry import PlatformEntry

        self.entries.append(PlatformEntry(
            name=name, label=label, adapter_factory=adapter_factory, check_fn=check_fn,
            validate_config=validate_config, required_env=required_env or [], install_hint=install_hint,
            **entry_kwargs,
        ))


def test_register_builds_real_platform_entry(load_plugin):
    plugin = load_plugin()
    ctx = Ctx()
    plugin.register(ctx)
    entry = ctx.entries[0]
    assert entry.name == "salesforce_chatter"
    assert entry.allow_update_command is False
    assert entry.max_message_length == 9000
    assert entry.allowed_users_env == "SF_CHATTER_ALLOWED_USERS"
    # Older supported cores have no display_tier; newer cores receive "minimal".
    assert getattr(entry, "display_tier", "minimal") == "minimal"


def test_validate_config_requires_env(load_plugin, monkeypatch):
    from gateway.config import PlatformConfig

    plugin = load_plugin()
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    assert plugin.adapter.validate_config(PlatformConfig(enabled=True, extra={})) is False
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert plugin.adapter.validate_config(PlatformConfig(enabled=True, extra={})) is True


def test_adapter_implements_all_abstract_methods(load_plugin, monkeypatch, tmp_path):
    from gateway.config import PlatformConfig

    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = load_plugin()
    adapter = plugin.adapter.SalesforceChatterAdapter(PlatformConfig(enabled=True, extra={}))
    assert not inspect.isabstract(type(adapter))
    for hook in ("on_processing_start", "on_processing_complete", "send", "connect", "disconnect", "get_chat_info"):
        assert inspect.iscoroutinefunction(getattr(adapter, hook))
