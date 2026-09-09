# Copyright (c) 2026, Frappe and Contributors
# See LICENSE
import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.payment_gateways.doctype.payment_demo_settings.payment_demo_settings import (
	PaymentDemoSettings,
)
from payments.payments.doctype.payment_session_log.payment_session_log import create_log
from payments.tests.fixtures import delete_all_buttons_and_commit
from payments.types import TxData
from payments.utils import PAYMENT_SESSION_REF_KEY
from payments.www.pay import default_icon, get_context, get_psl, load_icon


def _make_tx_data(**overrides) -> TxData:
	defaults = dict(
		amount=25.00,
		currency="EUR",
		reference_doctype="User",
		reference_docname="Administrator",
		payer_contact={},
		payer_address={},
		loyalty_points=None,
		discount_amount=None,
	)
	defaults.update(overrides)
	return TxData(**defaults)


def _ensure_button(label, **fields):
	"""Create a Payment Button fixture with exactly the given content.

	Deliberately delete-then-insert rather than `if not frappe.db.exists(...)`:
	the code under test commits (update_gateway_specific_state uses
	commit=True), which defeats IntegrationTestCase's per-class rollback, so a
	fixture from an earlier run can survive on a persistent test site. A
	conditional create would then silently reuse a stale template and the test
	would assert against content it did not write.
	"""
	frappe.delete_doc("Payment Button", label, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Payment Button",
			"label": label,
			"gateway_settings": "Payment Demo Settings",
			"gateway_controller": "Payment Demo Settings",
			**fields,
		}
	).insert(ignore_permissions=True)


def _disable_every_button(testcase, *, except_for=None):
	"""Disable every enabled Payment Button, and restore them afterwards.

	`except_for` keeps one button enabled, which is what makes "the chooser has
	nothing left to offer" a different state from "the payer's own selection was
	withdrawn". Those two were conflated: the earlier version disabled the
	SELECTED button too and still expected its widget to render — i.e. expected a
	disabled button to go on charging.

	The restore matters because the code under test commits, so otherwise this
	starves every later test of an enabled button.
	"""
	filters = {"enabled": 1}
	if except_for:
		filters["name"] = ("!=", except_for)
	enabled = frappe.get_all("Payment Button", filters=filters, pluck="name")

	def _reenable():
		for name in enabled:
			frappe.db.set_value("Payment Button", name, "enabled", 1)

	testcase.addCleanup(_reenable)
	for name in enabled:
		frappe.db.set_value("Payment Button", name, "enabled", 0)


class _FormDictMixin:
	"""/pay reads its parameters off frappe.local.form_dict; give each test a
	clean one and restore whatever the runner had."""

	def setUp(self):
		super().setUp()
		self._saved_form_dict = frappe.local.form_dict
		frappe.local.form_dict = frappe._dict()
		self.addCleanup(self._restore_form_dict)

	def _restore_form_dict(self):
		frappe.local.form_dict = self._saved_form_dict


class TestGetPsl(_FormDictMixin, IntegrationTestCase):
	"""/pay resolves its session from the `s` parameter.

	A missing or unknown reference must land on the invalid-link message page,
	not surface a raw KeyError / DoesNotExistError to a guest.
	"""

	def test_returns_psl_for_valid_reference(self):
		psl = create_log(tx_data=_make_tx_data())
		frappe.local.form_dict[PAYMENT_SESSION_REF_KEY] = psl.name
		self.assertEqual(get_psl().name, psl.name)

	def test_missing_reference_redirects(self):
		with patch("frappe.redirect_to_message") as redirect:
			with self.assertRaises(frappe.Redirect):
				get_psl()
		redirect.assert_called_once()

	def test_unknown_session_redirects(self):
		frappe.local.form_dict[PAYMENT_SESSION_REF_KEY] = "PSL-does-not-exist-0000"
		with patch("frappe.redirect_to_message") as redirect:
			with self.assertRaises(frappe.Redirect):
				get_psl()
		redirect.assert_called_once()


class TestLoadIcon(unittest.TestCase):
	def test_falls_back_to_default_icon_when_unset(self):
		self.assertEqual(load_icon(None), default_icon)
		self.assertEqual(load_icon(""), default_icon)


class TestPayContextDebugFlag(_FormDictMixin, IntegrationTestCase):
	"""`?debug=1` must require BOTH developer_mode AND an authenticated user.

	A guest must never receive debug output however the flag is set, since the
	page is public.
	"""

	def _render(self, *, user, developer_mode, debug="1"):
		psl = create_log(tx_data=_make_tx_data(), status="Paid")
		frappe.local.form_dict[PAYMENT_SESSION_REF_KEY] = psl.name
		frappe.local.form_dict["debug"] = debug
		context = frappe._dict()
		saved_user, saved_dev = frappe.session.user, frappe.conf.get("developer_mode")
		frappe.session.user = user
		frappe.conf["developer_mode"] = developer_mode
		try:
			get_context(context)
		finally:
			frappe.session.user = saved_user
			frappe.conf["developer_mode"] = saved_dev
		return context

	def test_guest_never_gets_debug(self):
		context = self._render(user="Guest", developer_mode=1)
		self.assertFalse(context.debug)

	def test_no_debug_without_developer_mode(self):
		context = self._render(user="Administrator", developer_mode=0)
		self.assertFalse(context.debug)

	def test_no_debug_without_the_flag(self):
		context = self._render(user="Administrator", developer_mode=1, debug="0")
		self.assertFalse(context.debug)

	def test_debug_for_authenticated_user_in_developer_mode(self):
		context = self._render(user="Administrator", developer_mode=1)
		self.assertTrue(context.debug)


class TestPayContextTerminalSession(_FormDictMixin, IntegrationTestCase):
	"""A session that already reached a terminal state shows the result only —
	no chooser, no widget, no capture form."""

	def test_terminal_session_renders_status_only(self):
		psl = create_log(tx_data=_make_tx_data(), status="Paid")
		frappe.local.form_dict[PAYMENT_SESSION_REF_KEY] = psl.name
		context = frappe._dict()
		get_context(context)
		self.assertFalse(context.render_widget)
		self.assertFalse(context.render_buttons)
		self.assertFalse(context.render_capture)
		self.assertEqual(context.status, "Paid")
		self.assertEqual(context.indicator_color, psl.get_indicator_color())
		self.assertEqual(context.tx_data.amount, 25.00)
		self.assertTrue(context.logo)


class TestPayContextButtonSelection(_FormDictMixin, IntegrationTestCase):
	"""First pass on a non-terminal session: render the chooser, and offer only
	enabled buttons."""

	ENABLED = "Test Pay Enabled Button"
	DISABLED = "Test Pay Disabled Button"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# No Payment Demo Settings fixture is needed: it is a Single, so it always
		# resolves through frappe.get_doc, and nothing here reads its fields.
		for label, enabled in ((cls.ENABLED, 1), (cls.DISABLED, 0)):
			_ensure_button(label, enabled=enabled, implementation_variant="Third Party Widget")

	def test_renders_chooser_with_enabled_buttons_only(self):
		psl = create_log(tx_data=_make_tx_data())
		frappe.local.form_dict[PAYMENT_SESSION_REF_KEY] = psl.name
		context = frappe._dict()
		get_context(context)

		self.assertTrue(context.render_buttons)
		self.assertFalse(context.render_widget)
		self.assertFalse(context.render_capture)

		labels = [label for _icon, _name, label in context.payment_buttons]
		self.assertIn(self.ENABLED, labels)
		self.assertNotIn(self.DISABLED, labels)

	def test_buttons_without_an_icon_get_the_default(self):
		psl = create_log(tx_data=_make_tx_data())
		frappe.local.form_dict[PAYMENT_SESSION_REF_KEY] = psl.name
		context = frappe._dict()
		get_context(context)

		icons = {name: icon for icon, name, _label in context.payment_buttons}
		self.assertEqual(icons[self.ENABLED], default_icon)


class TestPayContextDataCaptureFreshness(_FormDictMixin, IntegrationTestCase):
	"""Second pass (Data Capture): `pre_data_capture_hook` exists precisely so a
	gateway can fetch data the capture form needs, and it persists that data on the
	session log. The form must therefore be rendered from state read AFTER the hook
	ran — rendering the pre-hook snapshot defeats the hook's entire purpose, and
	does so silently, since the form still renders.
	"""

	BUTTON = "Test Pay Capture Button"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_button(
			cls.BUTTON,
			enabled=1,
			implementation_variant="Data Capture",
			extra_payload="{}",
			data_capture="<form data-probe='{{ psl.data_capture_payload }}'></form>",
		)

	def test_capture_form_sees_data_stored_by_the_hook(self):
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.BUTTON)
		frappe.local.form_dict[PAYMENT_SESSION_REF_KEY] = psl.name

		context = frappe._dict()
		with patch.object(PaymentDemoSettings, "_pre_data_capture_hook", return_value={"probe": "from-hook"}):
			get_context(context)

		self.assertTrue(context.render_capture)
		self.assertFalse(context.render_widget)
		self.assertIn(
			"from-hook",
			context.data_capture,
			"capture form was rendered from the pre-hook snapshot, so the gateway data "
			"the hook fetched never reached the template",
		)


def _fetch_pay_page(psl_name):
	"""Fetch /pay the way a payer's browser gets it, and return the response."""
	from frappe.website.serve import get_response

	frappe.local.form_dict = frappe._dict({PAYMENT_SESSION_REF_KEY: psl_name})
	return get_response("pay")


def _assert_rendered(testcase, response):
	"""Assert the page actually rendered, and return its body.

	The status code is the assertion that cannot be fooled. Grepping the body for
	"Traceback" / "UndefinedError" is NOT enough: frappe renders those only for a
	privileged session, so a Guest render of a broken page contains none of them
	and the grep passes on a 500 — for exactly the user /pay exists to serve.
	"""
	testcase.assertEqual(
		response.status_code, 200, f"the page failed to render (HTTP {response.status_code})"
	)
	return str(response.data, "utf-8")


def _unresolvable_scripts(html) -> list:
	"""Every same-origin `<script src>` in `html` that does not resolve to a file.

	/assets/** is served by the web server from sites/assets, NOT by frappe's
	website router, so get_response 404s on those even when they are correct —
	check the file on disk instead. Everything else goes through the router.
	"""
	import os
	import re

	from frappe.website.serve import get_response

	bad = []
	for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', html):
		if src.startswith(("http://", "https://", "//")):
			continue  # needs the network; out of scope here
		if src.startswith("/assets/"):
			if not os.path.exists(os.path.join(frappe.local.sites_path, src.lstrip("/"))):
				bad.append(src)
			continue
		frappe.local.form_dict = frappe._dict()
		if get_response(src.lstrip("/")).status_code != 200:
			bad.append(src)
	return bad


class TestPayPageRenders(_FormDictMixin, IntegrationTestCase):
	"""The HTTP/template layer, which the dict-level tests never reached."""

	BUTTON = "Test Render Button"
	WIDGET = "Test Render Widget Button"
	CAPTURE = "Test Render Capture Button"

	@classmethod
	def setUpClass(cls):
		# Registered BEFORE super() on purpose: class cleanups run LIFO and
		# IntegrationTestCase registers its _rollback_db inside setUpClass, so a
		# cleanup registered afterwards runs FIRST and the rollback then restores
		# every button it just deleted. That is why an enabled, XSS-payload-named
		# button survived a clean run.
		cls.addClassCleanup(delete_all_buttons_and_commit)
		super().setUpClass()
		# Own the button table for this class: leftovers from earlier runs are
		# enabled and would make the empty-chooser state unreachable.
		delete_all_buttons_and_commit()
		_ensure_button(cls.BUTTON, enabled=1, implementation_variant="Third Party Widget")
		_ensure_button(
			cls.WIDGET,
			enabled=1,
			implementation_variant="Third Party Widget",
			gateway_wrapper="<div id='WIDGET-MARKER'></div>",
		)
		_ensure_button(
			cls.CAPTURE,
			enabled=1,
			implementation_variant="Data Capture",
			extra_payload="{}",
			data_capture="<form id='CAPTURE-FORM-MARKER'></form>",
		)

	def test_renders_when_no_enabled_button_matches(self):
		"""The post-install state: no Payment Button exists at all, or the
		session's gateway filter matches none. render_buttons was set
		unconditionally and pay.html then dereferenced payment_buttons[0][1], so
		the payer's checkout page 500'd on the first end-to-end payment a fresh
		site ever attempts."""
		psl = create_log(tx_data=_make_tx_data())
		psl.db_set(
			"gateway",
			'{"gateway_settings": "No Such Settings", "gateway_controller": "No Such"}',
			commit=True,
		)
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn("no payment method", html.lower())

	def test_renders_the_chooser_when_a_button_matches(self):
		"""Control: the normal path must still offer the button."""
		psl = create_log(tx_data=_make_tx_data())
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn(self.BUTTON, html)

	def test_a_button_label_cannot_break_out_of_its_html_attribute(self):
		"""Payment Button uses autoname: field:label, so the docname IS the label,
		and it is interpolated into data-button="...". Frappe's website Jinja has
		autoescape OFF. The author is a System Manager, but the payload lands in
		the payer's browser on a checkout page."""
		_ensure_button(
			'Pay" onmouseover=alert(1) x="', enabled=1, implementation_variant="Third Party Widget"
		)
		psl = create_log(tx_data=_make_tx_data())
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertNotIn('data-button="Pay" onmouseover', html, "the label broke out of its attribute")

	def test_a_terminal_session_renders_its_result(self):
		"""Control on the template's other branch."""
		psl = create_log(tx_data=_make_tx_data(), status="Paid")
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn("Paid", html)
		# The terminal branch sets all three render flags False, so a fallback
		# derived from `not (render_widget or render_capture)` also fires here —
		# telling a payer who has just paid that no payment method is available,
		# on the one page where money has moved.
		self.assertNotIn("No payment method", html)

	def test_the_payer_name_reaches_the_page(self):
		"""The allowlist must not drop the parts a payer name is assembled from.

		The shape below is the one a contact-document producer emits: first_name
		and last_name with no full_name. Such a producer is prospective — no
		_get_contact_fields exists in the installed ERPNext v16.30.0 — but the
		projection dropped both parts, so any initiator supplying them rendered
		"Customer: None".
		"""
		erpnext_shape = {
			"first_name": "Ada",
			"last_name": "Lovelace",
			"email_id": "ada@example.com",
			"email": "ada@example.com",
			"phone": "+31600000000",
		}
		psl = create_log(tx_data=_make_tx_data(payer_contact=erpnext_shape))
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		# The full derived name, not just "Ada": asserting the first name alone
		# passes even if last_name was dropped.
		self.assertIn("Ada Lovelace", html, "the payer name did not reach the page")
		self.assertNotIn("Customer:&emsp;None", html)

	def test_a_capture_session_whose_button_was_disabled_still_renders_its_form(self):
		"""This test used to assert the OPPOSITE — that the page shows "no payment
		method is available" here — on the premise that render_capture rendered no
		form. The very commit that added that assertion also made pay.html render
		context.data_capture, which falsified the premise in the same change: the
		page now shows a working capture form AND denies a payment method, which is
		precisely the contradiction that commit fixed on the widget branch.

		The capture form IS the payment method. Only the chooser is gone, which is
		what losing the last enabled button means.
		"""
		# The controller must resolve, or pre_data_capture_hook throws a 417 before
		# anything renders — a different (pre-existing) failure than the one under
		# test here.
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.CAPTURE, commit=True)
		# Every OTHER button: the payer's own selection stays enabled, or this is
		# the different state where the selection itself was withdrawn — see
		# test_a_disabled_button_does_not_initiate_a_charge.
		_disable_every_button(self, except_for=self.CAPTURE)
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn("CAPTURE-FORM-MARKER", html, "the capture form did not render")
		self.assertNotIn(
			"No payment method",
			html,
			"the page rendered a working capture form and denied a payment method at the same time",
		)

	def test_an_empty_payer_contact_does_not_render_the_word_none(self):
		"""The allowlist fix was at the producer; the template is the last line of
		defence and printed the literal string None for any session without
		contact data."""
		psl = create_log(tx_data=_make_tx_data(payer_contact={}))
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertNotIn("&emsp;None", html)

	def test_every_button_label_is_escaped_not_just_the_first(self):
		"""Two hostile labels, so one lands in the secondary loop. The earlier
		version pinned only the primary slot, by accident of fixture ordering."""
		for label in ('Pay" onmouseover=alert(1) x="', 'Also" onfocus=alert(2) y="'):
			_ensure_button(label, enabled=1, implementation_variant="Third Party Widget")
		psl = create_log(tx_data=_make_tx_data())
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		# Assert the BREAK-OUT, not the payload: escaping turns the quote into
		# &quot; but leaves "onmouseover=alert(1)" present as inert text, so
		# asserting on the payload substring would fail either way.
		self.assertNotIn('data-button="Pay" onmouseover', html)
		self.assertNotIn('data-button="Also" onfocus', html)

	def test_a_widget_session_still_offers_the_chooser_and_no_denial(self):
		"""Control for the state below: a selected widget button, with other
		buttons still enabled."""
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.WIDGET, commit=True)
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn("WIDGET-MARKER", html)
		self.assertNotIn("No payment method", html)

	def test_a_widget_session_whose_button_was_disabled_does_not_deny_a_payment_method(self):
		"""The widget renders, so a payment method IS in play — but the denial was
		derived from `not buttons`, which is exactly `not render_buttons`, so it
		also fired here. The payer saw a live, working gateway widget with "No
		payment method is available for this transaction" underneath it.

		Reachable by an operator disabling the last button matching this session's
		gateway filter after the payer selected one.
		"""
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.WIDGET, commit=True)
		_disable_every_button(self, except_for=self.WIDGET)
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn("WIDGET-MARKER", html, "the widget did not render")
		self.assertNotIn(
			"No payment method",
			html,
			"the page rendered a working gateway widget and denied a payment method at the same time",
		)

	def test_a_capture_session_renders_its_capture_form(self):
		"""The missing control under the disabled-button test below: pay.py
		computes context.data_capture, but no template ever read it, so the Data
		Capture variant rendered no form in the GOOD case either — a whole button
		variant that never worked, with green tests on top of it."""
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.CAPTURE, commit=True)
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn("CAPTURE-FORM-MARKER", html, "the data capture form never reached the page")
		self.assertNotIn("No payment method", html)

	def test_the_page_renders_for_a_guest(self):
		"""/pay exists to serve an unauthenticated payer, and every other render
		test here runs as Administrator. _assert_rendered's status code is what
		makes this meaningful: a Guest's 500 page carries no "Traceback" marker."""
		psl = create_log(tx_data=_make_tx_data())
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn(self.BUTTON, html)

	def test_an_unreadable_gateway_filter_does_not_500_the_checkout_page(self):
		"""select_button already failed closed on a corrupt gateway restriction,
		but pay.py parsed the same value with a bare json.loads, so the payer's
		checkout page raised JSONDecodeError and served a 500. One fail-closed
		parser, three call sites."""
		psl = create_log(tx_data=_make_tx_data())
		psl.db_set("gateway", '{"gateway_settings": ', commit=True)  # truncated JSON
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertIn("no payment method", html.lower())
		self.assertNotIn(self.BUTTON, html, "an unreadable restriction offered buttons anyway")

	def test_a_gateway_restriction_cannot_widen_the_chooser(self):
		"""parse_gateway_ref validated the TYPE (a dict) but not the SHAPE, and
		pay.py did filters.update(restriction) — so a stored gateway value could
		override `enabled: True` and put disabled buttons in front of the payer.
		A restriction may only ever narrow.

		Not a live guest vector today: `gateway` is written server-side by
		create_log from a GatewayRef. It is a fail-OPEN parser guarding a money
		page, which is the same "one thing answering two questions" shape as the
		defects this branch keeps finding — get_controller validated the shape,
		the other two call sites did not.
		"""
		disabled = "Test Render Disabled Button"
		_ensure_button(disabled, enabled=0, implementation_variant="Third Party Widget")
		self.addCleanup(frappe.delete_doc, "Payment Button", disabled, force=True, ignore_permissions=True)

		psl = create_log(tx_data=_make_tx_data())
		psl.db_set("gateway", '{"enabled": 0}', commit=True)
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		self.assertNotIn(disabled, html, "a gateway restriction widened the chooser")

	def test_a_disabled_button_does_not_initiate_a_charge(self):
		"""select_button refuses a disabled button, but nothing re-checked it for a
		session already past the chooser — so rendering /pay went on to call
		proceed() and initiate a REAL gateway charge with a disabled button.

		An operator disabling a misconfigured or compromised gateway expects that
		to stop, so "disabled" has to mean disabled on every path, not just at
		selection time. The payer keeps the other methods.
		"""
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.WIDGET, commit=True)
		frappe.db.set_value("Payment Button", self.WIDGET, "enabled", 0)
		self.addCleanup(frappe.db.set_value, "Payment Button", self.WIDGET, "enabled", 1)

		html = _assert_rendered(self, _fetch_pay_page(psl.name))

		psl.reload()
		self.assertIsNone(
			psl.initiation_response_payload,
			"a disabled button initiated a real gateway charge",
		)
		self.assertEqual(psl.status, "Created")
		self.assertNotIn("WIDGET-MARKER", html, "the disabled button's widget rendered anyway")
		# The other enabled buttons are still on offer.
		self.assertIn(self.BUTTON, html)

	def test_every_script_the_page_loads_actually_resolves(self):
		"""pay.html overrides base_scripts, so it names frappe's web bundle itself
		— and it named one that does not exist ('website-core.bundle.js'), which
		404s. base.html's head shim only QUEUES frappe.ready callbacks; the bundle
		is what flushes that queue. So with no bundle the inlined pay.js queued its
		click handlers and nothing ever ran them: the payment method chooser did
		nothing at all in a browser.

		_assert_rendered cannot see this — the HTML is a perfectly good 200. The
		control below is what makes this assertion mean anything: the same
		resolution applied to a stock frappe page must pass.
		"""
		psl = create_log(tx_data=_make_tx_data())
		html = _assert_rendered(self, _fetch_pay_page(psl.name))
		unresolved = _unresolvable_scripts(html)
		self.assertEqual(unresolved, [], f"/pay loads scripts that do not resolve: {unresolved}")

	def test_the_script_resolution_control(self):
		"""Control for the test above: a stock frappe page's scripts must resolve
		by the same means, or a green result there says nothing."""
		frappe.local.form_dict = frappe._dict()
		from frappe.website.serve import get_response

		html = str(get_response("login").data, "utf-8")
		self.assertIn("frappe-web.bundle", html, "the control page did not load the bundle")
		self.assertEqual(_unresolvable_scripts(html), [], "the resolver rejects a correct page")
