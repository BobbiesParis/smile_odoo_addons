"""Refuse to record a half-applied upgrade as successful.

Odoo does not raise when a module cannot be loaded: ModuleGraph only logs
"module X: some depends are not loaded (Y), skipped" and load_modules() ends with
"Some modules have inconsistent states, some dependencies may be missing". As an
upgrade failure is detected through exceptions only, such a run would still reach
UpgradeManager.set_db_version(), write code.version, and exit 0 — the next boot
then finds db_version == code_version, logs "no upgrade to load", and the modules
stay stuck in 'to upgrade' forever.

This check runs before code.version is written, so a partial upgrade is reported
as FAILED and replayed on the next boot.
"""

import logging

_logger = logging.getLogger(__package__)

# Same states as the core check at the end of odoo.modules.loading.load_modules().
_PENDING_STATES = ("to install", "to upgrade")

# States for which a module is picked up by ModuleGraph. A dependency in any other
# state ('uninstalled', 'uninstallable', or with no row at all) makes every module
# depending on it — recursively — be dropped from the graph.
_LOADABLE_STATES = ("installed", "to install", "to upgrade")

# Reported in place of a dependency name when the module itself has no manifest
# left on disk: nothing can be installed to fix that one.
_NO_MANIFEST = "<manifest>"


def _modules_in_state(cr, states):
    cr.execute(
        "SELECT name, state FROM ir_module_module WHERE state IN %s ORDER BY state, name",
        [tuple(states)],
    )
    return cr.fetchall()


def _blocking_dependencies(cr):
    """Return the (module, dependency, dependency_state) triplets that keep modules
    out of the module graph — the root causes of a skip cascade.

    Dependencies are read from the manifests on disk, like ModuleGraph does, and not
    from ir_module_module_dependency: that table is only refreshed by
    ir.module.module.update_list(), so on a load-only run it still reflects the
    manifests of the previous code version.
    """
    from odoo.modules import Manifest  # noqa: PLC0415

    cr.execute("SELECT name, state FROM ir_module_module")
    states = dict(cr.fetchall())

    blocking = []
    for name, state in sorted(states.items()):
        if state not in _LOADABLE_STATES:
            continue
        manifest = Manifest.for_addon(name, display_warning=False)
        if manifest is None:
            blocking.append((name, _NO_MANIFEST, "missing"))
            continue
        for dep in manifest["depends"]:
            dep_state = states.get(dep, "unknown")
            if dep_state not in _LOADABLE_STATES:
                blocking.append((name, dep, dep_state))
    return blocking


def assert_modules_consistent(cr):
    """Raise if some modules were never installed/upgraded."""
    to_remove = _modules_in_state(cr, ("to remove",))
    if to_remove:
        # Not fatal: only an update run uninstalls modules, and core does not count
        # 'to remove' as an inconsistency either.
        _logger.warning(
            "Upgrade: %d module(s) still marked for removal: %s",
            len(to_remove),
            ", ".join(name for name, _state in to_remove),
        )

    pending = _modules_in_state(cr, _PENDING_STATES)
    if not pending:
        _logger.info("Upgrade: all modules are in a consistent state")
        return

    pending_names = ", ".join(f"{name} ({state})" for name, state in pending)
    lines = [
        f"Upgrade aborted: {len(pending)} module(s) were not installed/upgraded and are "
        "left in an inconsistent state, so code.version is not updated.",
        f"Pending modules: {pending_names}",
    ]
    if blocking := _blocking_dependencies(cr):
        blocking_names = ", ".join(f"{name} -> {dep} ({state})" for name, dep, state in blocking)
        lines.append(
            f"Root cause — dependencies that never entered the module graph: {blocking_names}"
        )
        # `-u all` (i.e. `-u base`) is NOT enough: button_upgrade() only scans modules
        # in state 'installed', so it never looks at the dependencies of a module
        # already stuck in 'to upgrade' — the very modules that need them.
        to_install = sorted({dep for _name, dep, _state in blocking if dep != _NO_MANIFEST})
        if to_install:
            lines.append(
                "Add them to `modules_to_install` in the upgrade's __upgrade__.py "
                f"(or pass `-i {','.join(to_install)}`) and replay the upgrade; "
                "`-u all` would not help."
            )
    else:
        lines.append(
            "No unmet dependency found: look for skipped modules in the loading log "
            "(dependency loop, non-installable manifest), then replay the upgrade."
        )

    message = "\n".join(lines)
    _logger.error(message)
    # Plain RuntimeError on purpose: _manage_upgrade_errors() must be able to log the
    # message as-is, and Odoo 19's UserError no longer exposes `.value`.
    raise RuntimeError(message)
