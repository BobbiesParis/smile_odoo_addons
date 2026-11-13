from unittest.mock import patch

from odoo.modules.registry import Registry
from odoo.tests import TransactionCase, tagged
from odoo.tools import config

from odoo.addons.smile_upgrade.models import registry as smile_registry


class StubUpgradeManager:
    """Minimal stand-in for UpgradeManager: the real one opens its own cursor,
    takes an advisory lock, and runs the upgrade files. `cr` is filled in by the
    test case, as _run_upgrade_post() runs the consistency check on it."""

    def __init__(self, db_in_creation=False):
        self.cr = None
        self.db_in_creation = db_in_creation
        self.code_version = "9.9.9"
        self.upgrades = ["<upgrade>"]
        self.modules_to_upgrade = ["base"]
        self.modules_to_install = ["bobbies_barcode"]
        self.modules_to_install_at_creation = ["bobbies_app"]
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def _get_upgrades(self):
        return self.upgrades

    def pre_load(self):
        self.calls.append("pre_load")

    def post_load(self):
        self.calls.append("post_load")

    def reload_translations(self):
        self.calls.append("reload_translations")

    def set_db_version(self):
        self.calls.append("set_db_version")


@tagged("post_install", "-at_install", "continuous_integration", "smile_upgrade")
class TestUpgradeFlow(TransactionCase):
    """smile_upgrade must actually upgrade the modules.

    Odoo 19 installs/upgrades only when `update_module` is set, and takes the
    module lists from the Registry.new() parameters — it never reads
    config['init'] / config['update'] any more.
    """

    def setUp(self):
        super().setUp()
        # Both the success and the failure path call os._exit() when this is on.
        original = config.get("stop_after_upgrades")
        config["stop_after_upgrades"] = False
        self.addCleanup(config.__setitem__, "stop_after_upgrades", original)
        # _run_upgrade_post() refuses to finish on a half-applied upgrade: keep the
        # ambient state of the test database out of the assertions (rolled back).
        self.env.cr.execute(
            "UPDATE ir_module_module SET state = 'installed' "
            "WHERE state IN ('to install', 'to upgrade')"
        )

    def _call_new(self, upgrade_manager, **kwargs):
        """Call smile_upgrade's Registry.new() the way the server does.

        smile_upgrade only takes over when 'preload_registries' or '_initialize_db'
        is in the call stack, hence the wrapper function's name. Its function is
        called directly rather than through Registry.new, because bobbies_infra
        wraps that attribute with its own layer (database neutralisation), which
        would try to connect to the fictitious database used here.
        """
        native_calls = []
        upgrade_manager.cr = self.env.cr

        def fake_native_new(**call_kwargs):
            native_calls.append(call_kwargs)
            return "<registry>"

        def preload_registries():
            return smile_registry.new.__func__(Registry, "test_upgrade_flow", **kwargs)

        with (
            patch.object(smile_registry, "native_new", fake_native_new),
            patch.object(smile_registry, "UpgradeManager", lambda db_name: upgrade_manager),
        ):
            self.assertEqual(preload_registries(), "<registry>")
        return native_calls

    def test_upgrade_pass_forces_update_module(self):
        """Without this, modules are loaded but never upgraded — they stay in
        state 'to upgrade' and the database is silently left half-migrated."""
        manager = StubUpgradeManager()

        upgrade_call, final_call = self._call_new(manager, update_module=False)

        self.assertIs(upgrade_call["update_module"], True)
        self.assertEqual(list(upgrade_call["upgrade_modules"]), ["base"])
        self.assertIn("pre_load", manager.calls)
        self.assertIn("set_db_version", manager.calls)
        # The final call is a plain registry load, with the caller's own values.
        self.assertFalse(final_call["update_module"])

    def test_upgrade_scope_does_not_leak_into_the_final_load(self):
        """The upgrade list must not reach the load that follows: it would mark
        'base' as 'to upgrade' again (loading.py) with nothing to flip it back."""
        manager = StubUpgradeManager()

        upgrade_call, final_call = self._call_new(
            manager, update_module=False, upgrade_modules={}, install_modules={}
        )

        self.assertEqual(list(upgrade_call["upgrade_modules"]), ["base"])
        self.assertEqual(list(final_call["upgrade_modules"]), [])
        self.assertEqual(config["update"], {})
        self.assertEqual(config["init"], {})

    def test_new_dependencies_are_installed(self):
        """`modules_to_install` reaches install_modules, so button_install() runs on
        the new v19 dependencies — the only way to install a dependency of a module
        already stuck in 'to upgrade'."""
        manager = StubUpgradeManager()

        upgrade_call, final_call = self._call_new(manager, update_module=False)

        self.assertEqual(list(upgrade_call["install_modules"]), ["bobbies_barcode"])
        # And it must not leak into the plain load that follows.
        self.assertEqual(list(final_call["install_modules"]), [])

    def test_half_applied_upgrade_never_reaches_set_db_version(self):
        """The consistency check runs inside _run_upgrade_post: a module left behind
        aborts the upgrade, so code.version keeps its old value and the next boot
        replays the upgrade instead of finding "no upgrade to load"."""
        manager = StubUpgradeManager()
        self.env.cr.execute(
            "UPDATE ir_module_module SET state = 'to upgrade' WHERE name = 'smile_upgrade'"
        )

        with self.assertRaises(RuntimeError) as error:
            self._call_new(manager, update_module=False)

        self.assertIn("smile_upgrade (to upgrade)", str(error.exception))
        self.assertNotIn("set_db_version", manager.calls)
        self.assertNotIn("post_load", manager.calls)

    def test_database_in_creation_installs_the_declared_modules(self):
        manager = StubUpgradeManager(db_in_creation=True)

        upgrade_call, _final_call = self._call_new(manager, update_module=True)

        self.assertEqual(list(upgrade_call["install_modules"]), ["bobbies_app"])
        self.assertEqual(list(upgrade_call["upgrade_modules"]), [])
        # A database being created has no upgrade file to replay.
        self.assertNotIn("pre_load", manager.calls)

    def test_caller_lists_are_preserved(self):
        """An explicit `-u`/`-i` on the command line must still be honoured."""
        manager = StubUpgradeManager()

        upgrade_call, final_call = self._call_new(
            manager,
            update_module=True,
            install_modules={"bobbies_pim": True},
            upgrade_modules={"bobbies_stock": True},
        )

        self.assertEqual(
            sorted(upgrade_call["install_modules"]), ["bobbies_barcode", "bobbies_pim"]
        )
        self.assertEqual(sorted(upgrade_call["upgrade_modules"]), ["base", "bobbies_stock"])
        self.assertEqual(list(final_call["upgrade_modules"]), ["bobbies_stock"])

    def test_no_upgrade_to_load_is_a_plain_registry_load(self):
        manager = StubUpgradeManager()
        manager.upgrades = []

        calls = self._call_new(manager, update_module=False)

        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["update_module"])
        self.assertEqual(manager.calls, [])
