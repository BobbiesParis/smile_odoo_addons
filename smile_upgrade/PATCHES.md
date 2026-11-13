# Bobbies patches on `smile_upgrade`

This module is vendored from Smile and **has been patched locally**. The changes below
are not Bobbies-specific business logic: four of them are Odoo 19 porting bugs that
made the upgrade mechanism a no-op, the fifth adds the missing safety net. They are
meant to go upstream — keep this file in sync so the divergence stays visible and
re-appliable after a vendor update.

The module name is deliberately **not** renamed: it is an identifier in
`ir_module_module`, in `ir_model_data.module`, in the dependency table, and in
`server_wide_modules` (SOPS-encrypted config). Renaming it without updating the
encrypted config in lockstep would silently stop the module from being `--load`ed,
which disables the whole upgrade mechanism without any error.

Patches keep the module's AGPL-3 license and Smile's authorship.

---

## 1. The upgrade pass never upgraded anything

**File** `models/registry.py` — `new()`

**Symptom** — `Some modules have inconsistent states, some dependencies may be missing: [...]`
at every boot, modules stuck in `to upgrade`, and `code.version` already bumped.

**Cause** — v14 forced the update on the upgrade pass:
`native_new(db_name, force_demo, status, True)`. The v19 port passes on the flag it
received, and `preload_registries()` computes that flag from
`config['init'] or config['update'] or config['reinit']` **before** `_run_upgrade_pre()`
fills `config["update"]` — so it is `False` whenever the upgrade is triggered by
`upgrades_path` alone (no `-u` / `-i` on the command line, which is the case for the
Kubernetes `odoo-upgrade` Job). The module graph is then built in `mode='load'`:
modules are loaded but `update_operation` stays `None`, so no state is ever flipped
back to `installed`.

**Change** — the upgrade pass passes `update_module=True`.

**Verified** — same command, same database, only this patch differing: without it a
module in `to upgrade` is never loaded for update; with it, it is upgraded and the
upgrade completes (`smile_upgrade/tests/test_upgrade_flow.py`).

## 2. Module lists injected into the config are ignored

**File** `models/registry.py` — `_run_upgrade_pre()`, `_get_initial_config()`,
`_run_upgrade_post()`

**Cause** — since Odoo 19, `load_modules()` takes `install_modules` / `upgrade_modules`
as **parameters** and never reads `config['init']` / `config['update']`. Injecting the
lists into the config therefore had no effect at all: both `modules_to_upgrade` and
`modules_to_install_at_creation` were silently ignored. Worse, `config.__getitem__`
returns the very dicts `Registry.new()` received, so mutating them leaked the upgrade
scope into the plain registry load that follows the upgrade, leaving `base` marked
`to upgrade`.

**Change** — `_run_upgrade_pre()` **returns** the `(install, upgrade)` lists; they are
merged into the parameters of the upgrade pass only. `_get_initial_config()` /
`_run_upgrade_post()` no longer save and restore `config['init']` / `config['update']`
(only `max_cron_threads`).

## 3. `UserError.value` no longer exists in v19

**File** `models/registry.py` — `_manage_upgrade_errors()`

**Cause** — `msg = (isinstance(e, UserError) and e.value) or e` raised an
`AttributeError` inside the error handler, masking the actual upgrade failure exactly
when it mattered.

**Change** — log `str(e)` directly; the `UserError` import is gone.

## 4. New dependency modules cannot be installed by an upgrade

**Files** `models/upgrade.py` (`UpgradeManager.modules_to_install`, `Upgrade`
default values), `models/registry.py`

**Cause** — when a new version adds a dependency to an already installed module,
`button_upgrade()` cannot discover it: it only scans modules in state `installed`
(`ir_module.py`), so it never looks at the dependencies of a module left in
`to upgrade` — precisely the modules that need them. `__upgrade__.py` had no way to
express "install these modules on an existing database" (`modules_to_install_at_creation`
only applies to a database being created).

**Change** — new `modules_to_install` key in `__upgrade__.py`, aggregated over the
applicable upgrades and forwarded as `install_modules`, so `button_install()` handles
it after `update_list()` — which also works for a module that has no
`ir_module_module` row yet in a freshly restored dump.

## 5. A half-applied upgrade was recorded as successful

**Files** `models/consistency.py` (new), `models/registry.py`
(`_run_upgrade_post()`), `models/__init__.py`

**Cause** — Odoo never raises when a module cannot be loaded (`ModuleGraph` logs
`module X: some depends are not loaded (Y), skipped`, `load_modules()` logs
`Some modules have inconsistent states`). As an upgrade failure is only detected
through exceptions, such a run still reached `set_db_version()`, wrote `code.version`,
and exited 0 — the next boot then found `db_version == code_version`, logged
`no upgrade to load`, and the modules stayed stuck forever.

**Change** — `_run_upgrade_post()` starts with `assert_modules_consistent()`, which
raises while modules remain in `to install` / `to upgrade`, before the post-load files
and before `set_db_version()`. The error names the root cause: dependencies that never
entered the module graph, read from the **manifests on disk** (the
`ir_module_module_dependency` table is only refreshed by `update_list()`, so it is
stale on a load-only run).

---

## Maintenance

- Tests: `smile_upgrade/tests/` (tagged `continuous_integration`). Run them with
  `--test-tags /smile_upgrade`.
- After pulling a new vendor version, re-apply these five patches and re-run the tests;
  patches 1 to 3 are pure bug fixes and should be dropped as soon as they land upstream.
- Bobbies-side consumers to keep in mind: `bobbies_infra` depends on this module,
  `upgrades/2.0.0/__upgrade__.py` uses `modules_to_install`, and
  `upgrades/2.0.0/pre_migration_scripts.sql` matches on `%smile_upgrade%`.
