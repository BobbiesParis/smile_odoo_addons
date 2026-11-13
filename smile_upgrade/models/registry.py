from collections.abc import Collection
import inspect
import logging
import os
import time

from odoo.modules.registry import Registry
from odoo.tools import config

from .consistency import assert_modules_consistent
from .upgrade import UpgradeManager

_logger = logging.getLogger(__name__)

native_new = Registry.new

# Databases whose upgrade pass is running in this process. Once it has
# uninstalled the modules marked 'to remove', load_modules() reloads the
# registry recursively (loading.py, STEP 5), still under preload_registries.
# That nested call must go straight to Odoo: a second UpgradeManager would
# open another connection and wait forever on the advisory lock the first one
# holds.
_upgrading_dbs = set()


@classmethod
def new(
    cls,
    db_name: str,
    *,
    update_module: bool = False,
    install_modules: Collection[str] = (),
    upgrade_modules: Collection[str] = (),
    reinit_modules: Collection[str] = (),
    new_db_demo: bool | None = None,
    models_to_check: set[str] | None = None,
):
    callers = [frame[3] for frame in inspect.stack()]
    preloading = (
        "preload_registries" in callers or "_initialize_db" in callers
    )
    if db_name in _upgrading_dbs or not preloading:
        return native_new(
            db_name=db_name,
            update_module=update_module,
            install_modules=install_modules,
            upgrade_modules=upgrade_modules,
            reinit_modules=reinit_modules,
            new_db_demo=new_db_demo,
            models_to_check=models_to_check,
        )
    with cls._lock:
        upgrades = False
        try:
            with UpgradeManager(db_name) as upgrade_manager:
                upgrades = upgrade_manager.upgrades
                #  Get upgrade twice to refresh value if the first call was
                #  made before advisory_lock.
                #  This can lead to n upgrade in multi front configuration.
                if upgrades and upgrade_manager._get_upgrades():
                    t0 = time.time()
                    _logger.info("loading %s upgrade...", upgrade_manager.code_version)
                    max_cron_threads = _get_initial_config()
                    to_install, to_upgrade = _run_upgrade_pre(upgrade_manager)
                    _upgrading_dbs.add(db_name)
                    try:
                        native_new(
                            db_name=db_name,
                            # Forced: Odoo only installs/upgrades modules when this flag
                            # is set, and preload_registries() computes it from
                            # `config['init'] or config['update']` — empty when the
                            # upgrade is triggered by upgrades_path alone. Passing the
                            # caller's value here would load the modules without ever
                            # upgrading them, leaving them in state 'to upgrade'.
                            update_module=True,
                            install_modules=[*install_modules, *to_install],
                            upgrade_modules=[*upgrade_modules, *to_upgrade],
                            reinit_modules=reinit_modules,
                            new_db_demo=new_db_demo,
                            models_to_check=models_to_check,
                        )
                    finally:
                        _upgrading_dbs.discard(db_name)
                    _run_upgrade_post(upgrade_manager, max_cron_threads)
                    _logger.info(
                        "%s upgrade successfully loaded in %ss", upgrade_manager.code_version, time.time() - t0
                    )
                else:
                    _logger.info("no upgrade to load")
            # Remove base from "init", to avoid update of the module
            config["init"].pop("base", None)
            registry = native_new(
                db_name=db_name,
                update_module=update_module,
                install_modules=install_modules,
                upgrade_modules=upgrade_modules,
                reinit_modules=reinit_modules,
                new_db_demo=new_db_demo,
                models_to_check=models_to_check,
            )
            if upgrades and str_to_bool(config.get("stop_after_upgrades")):
                _logger.info("Stopping Odoo server")
                os._exit(0)
            return registry
        except Exception as e:
            _manage_upgrade_errors(upgrades, e)
            raise


def str_to_bool(val):
    return str(val).lower() in ("true", "1", "yes", "y")


def _manage_upgrade_errors(upgrades, e):
    if upgrades and str_to_bool(config.get("stop_after_upgrades")):
        # Odoo 19's UserError no longer exposes `.value`: reading it here would
        # raise an AttributeError and hide the actual upgrade failure.
        _logger.error(str(e), exc_info=True)
        _logger.critical("Upgrade FAILED")
        _logger.info("Stopping Odoo server")
        os._exit(1)


def _get_initial_config():
    return config.get("max_cron_threads")


def _run_upgrade_pre(upgrade_manager):
    """Run the pre-load files and return the (install, upgrade) module lists.

    The lists are returned instead of being injected into config["init"] /
    config["update"]: since Odoo 19, load_modules() takes them as parameters and
    never reads the config, so injecting them there has no effect at all. They
    must also stay out of the config because Registry.new() receives the very
    dicts config['init']/config['update'] hold — mutating them would leak the
    upgrade scope into the plain registry load that follows the upgrade.
    """
    config["max_cron_threads"] = 0
    if upgrade_manager.db_in_creation:
        return list(upgrade_manager.modules_to_install_at_creation), []
    upgrade_manager.pre_load()
    return list(upgrade_manager.modules_to_install), list(upgrade_manager.modules_to_upgrade)


def _run_upgrade_post(upgrade_manager, max_cron_threads):
    # First: a partially applied upgrade must not reach set_db_version() below, nor
    # waste time in the post-load files (which usually only look at installed modules).
    assert_modules_consistent(upgrade_manager.cr)
    upgrade_manager.post_load()
    upgrade_manager.reload_translations()
    upgrade_manager.set_db_version()
    config["max_cron_threads"] = max_cron_threads


Registry.new = new
