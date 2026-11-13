from odoo.tests import TransactionCase, tagged

from odoo.addons.smile_upgrade.models import consistency


@tagged("post_install", "-at_install", "continuous_integration", "smile_upgrade")
class TestConsistency(TransactionCase):
    """The check that prevents an upgrade from being recorded as successful while
    modules are left behind.

    Every state change below is plain SQL rolled back with the test transaction —
    the check reads ir_module_module directly, so no ORM flush is involved.
    """

    def _set_state(self, module_name, state):
        self.env.cr.execute(
            "UPDATE ir_module_module SET state = %s WHERE name = %s",
            (state, module_name),
        )

    def _make_consistent(self):
        """Neutralise the ambient state of the test database so each assertion
        depends only on what the test itself sets up."""
        self.env.cr.execute(
            "UPDATE ir_module_module SET state = 'installed' "
            "WHERE state IN ('to install', 'to upgrade')"
        )

    def test_consistent_database_passes(self):
        self._make_consistent()
        # Must not raise.
        consistency.assert_modules_consistent(self.env.cr)

    def test_pending_module_aborts(self):
        self._make_consistent()
        self._set_state("smile_upgrade", "to upgrade")

        with self.assertRaises(RuntimeError) as error:
            consistency.assert_modules_consistent(self.env.cr)
        message = str(error.exception)
        self.assertIn("smile_upgrade (to upgrade)", message)
        self.assertIn("code.version is not updated", message)

    def test_blocking_dependency_is_reported(self):
        """The root cause — a dependency that never enters the module graph — is
        named in the error, not just the cascade of pending modules."""
        self._make_consistent()
        # smile_upgrade declares web in its manifest.
        self._set_state("web", "uninstalled")
        self._set_state("smile_upgrade", "to upgrade")

        with self.assertRaises(RuntimeError) as error:
            consistency.assert_modules_consistent(self.env.cr)
        message = str(error.exception)
        self.assertIn("smile_upgrade -> web (uninstalled)", message)
        # The actionable hint: the dependency has to be installed explicitly. It
        # also carries whatever else the database is missing, hence the substring.
        hint = next(line for line in message.splitlines() if "modules_to_install" in line)
        self.assertIn("web", hint.partition("`")[2])

    def test_dependencies_are_read_from_the_manifests(self):
        """ir_module_module_dependency is only refreshed by update_list(), so the
        check must not rely on it: a stale row must not hide the real dependency."""
        self._make_consistent()
        self._set_state("web", "uninstalled")
        self._set_state("smile_upgrade", "to upgrade")
        self.env.cr.execute(
            "DELETE FROM ir_module_module_dependency WHERE module_id = "
            "(SELECT id FROM ir_module_module WHERE name = 'smile_upgrade')"
        )

        with self.assertRaises(RuntimeError) as error:
            consistency.assert_modules_consistent(self.env.cr)
        self.assertIn("smile_upgrade -> web", str(error.exception))

    def test_module_without_manifest_is_reported(self):
        """A module installed in the database but gone from the code base keeps
        every dependent module out of the graph too."""
        self._make_consistent()
        self.env["ir.module.module"].create(
            {"name": "module_gone_from_disk", "state": "installed"}
        )
        self.env.flush_all()
        self._set_state("smile_upgrade", "to upgrade")

        with self.assertRaises(RuntimeError) as error:
            consistency.assert_modules_consistent(self.env.cr)
        self.assertIn("module_gone_from_disk -> <manifest> (missing)", str(error.exception))

    def test_modules_to_remove_are_not_fatal(self):
        self._make_consistent()
        self._set_state("smile_upgrade", "to remove")

        # 'to remove' is only warned about: core does not treat it as an
        # inconsistency, and only an update run uninstalls modules.
        consistency.assert_modules_consistent(self.env.cr)
