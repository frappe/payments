# Copyright (c) 2026, Frappe and Contributors
# See LICENSE
"""Test fixture helpers that must COMMIT.

They live outside a `test_*.py` module on purpose: semgrep's local
`test-correctness.yml` (`Dont-commit`) forbids `frappe.db.commit()` inside a
test module, and these helpers genuinely need it — see below.
"""

import frappe


def delete_all_buttons_and_commit():
	"""Remove every Payment Button, and commit the removal.

	The commit is load-bearing in BOTH directions:

	- As setup, because the code under test commits (`update_gateway_specific_state`
	  uses `commit=True`), which defeats IntegrationTestCase's per-class rollback:
	  buttons from an earlier run survive on a persistent site, stay enabled, and
	  silently make "no enabled button matches" unreachable.
	- As a class cleanup, because the rollback would otherwise undo the deletion.
	  IntegrationTestCase registers `_rollback_db` via `addClassCleanup` in its own
	  `setUpClass`, and class cleanups run LIFO — so a cleanup registered *after*
	  `super().setUpClass()` runs BEFORE the rollback, and the rollback puts every
	  button straight back. That is how an enabled, XSS-payload-named button
	  survived a clean run and was left for later modules to render. Register this
	  before `super().setUpClass()`.
	"""
	for name in frappe.get_all("Payment Button", pluck="name"):
		frappe.delete_doc("Payment Button", name, force=True, ignore_permissions=True)
	frappe.db.commit()
