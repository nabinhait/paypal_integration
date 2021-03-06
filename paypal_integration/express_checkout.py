# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from __future__ import unicode_literals
import frappe
from frappe.utils import get_url
from urllib import urlencode
import urlparse, json
from frappe import _
from frappe.utils import get_request_session

"""
Paypal Express Checkout using classic API

For full workflow:

https://developer.paypal.com/docs/classic/express-checkout/ht_ec-singleItemPayment-curl-etc/
"""

class PaypalException(Exception): pass

@frappe.whitelist(allow_guest=True, xss_safe=True)
def set_express_checkout(amount, currency="USD", data=None):
	validate_transaction_currency(currency)

	if not isinstance(data, basestring):
		data = json.dumps(data or "{}")

	response = execute_set_express_checkout(amount, currency)

	paypal_settings = get_paypal_settings()
	if paypal_settings.paypal_sandbox:
		return_url = "https://www.sandbox.paypal.com/cgi-bin/webscr?cmd=_express-checkout&token={0}"
	else:
		return_url = "https://www.paypal.com/cgi-bin/webscr?cmd=_express-checkout&token={0}"

	token = response.get("TOKEN")[0]
	paypal_express_payment = frappe.get_doc({
		"doctype": "Paypal Express Payment",
		"status": "Started",
		"amount": amount,
		"currency": currency,
		"token": token,
		"data": data
	})
	if data:
		data = json.loads(data)
		if data.get("doctype") and  data.get("docname"):
			paypal_express_payment.reference_doctype = data.get("doctype")
			paypal_express_payment.reference_docname = data.get("docname")

	paypal_express_payment.insert(ignore_permissions = True)
	frappe.db.commit()

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = return_url.format(token)

def execute_set_express_checkout(amount, currency):
	params = get_paypal_params()
	params.update({
		"METHOD": "SetExpressCheckout",
		"PAYMENTREQUEST_0_PAYMENTACTION": "SALE",
		"PAYMENTREQUEST_0_AMT": amount,
		"PAYMENTREQUEST_0_CURRENCYCODE": currency
	})

	return_url = get_url("/api/method/paypal_integration.express_checkout.get_express_checkout_details")

	params = urlencode(params) + \
		"&returnUrl={0}&cancelUrl={1}".format(return_url, get_url("/paypal-express-cancel"))

	return get_api_response(params.encode("utf-8"))

@frappe.whitelist(allow_guest=True, xss_safe=True)
def get_express_checkout_details(token):
	params = get_paypal_params()
	params.update({
		"METHOD": "GetExpressCheckoutDetails",
		"TOKEN": token
	})
	
	response = get_api_response(params)

	paypal_express_payment = frappe.get_doc("Paypal Express Payment", token)
	paypal_express_payment.payerid = response.get("PAYERID")[0]
	paypal_express_payment.payer_email = response.get("EMAIL")[0]
	paypal_express_payment.status = "Verified"
	paypal_express_payment.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = get_url( \
		"/api/method/paypal_integration.express_checkout.confirm_payment?token="+paypal_express_payment.token)

@frappe.whitelist(allow_guest=True, xss_safe=True)
def confirm_payment(token):
	paypal_express_payment = frappe.get_doc("Paypal Express Payment", token)

	params = get_paypal_params()
	params.update({
		"METHOD": "DoExpressCheckoutPayment",
		"PAYERID": paypal_express_payment.payerid,
		"TOKEN": paypal_express_payment.token,
		"PAYMENTREQUEST_0_PAYMENTACTION": "SALE",
		"PAYMENTREQUEST_0_AMT": paypal_express_payment.amount,
		"PAYMENTREQUEST_0_CURRENCYCODE": paypal_express_payment.currency
	})

	try:
		response = get_api_response(params)

	except PaypalException, e:
		frappe.db.rollback()
		frappe.get_doc({
			"doctype": "Paypal Log",
			"error": "{e}\n\n{traceback}".format(e=e, traceback=frappe.get_traceback()),
			"params": frappe.as_json(params)
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url("/paypal-express-failed")

	else:
		paypal_express_payment = frappe.get_doc("Paypal Express Payment", token)
		paypal_express_payment.status = "Completed"
		paypal_express_payment.transaction_id = response.get("PAYMENTINFO_0_TRANSACTIONID")[0]
		paypal_express_payment.correlation_id = response.get("CORRELATIONID")[0]
		paypal_express_payment.save(ignore_permissions=True)
		trigger_ref_doc(paypal_express_payment, "set_as_paid")
		frappe.db.commit()

		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url("/paypal-express-success")

def get_paypal_params():
	paypal_settings = get_paypal_settings()
	if paypal_settings.api_username:
		return {
			"USER": paypal_settings.api_username,
			"PWD": paypal_settings.api_password,
			"SIGNATURE": paypal_settings.signature,
			"VERSION": "98"
		}

	else :
		return {
			"USER": frappe.conf.paypal_username,
			"PWD": frappe.conf.paypal_password,
			"SIGNATURE": frappe.conf.paypal_signature,
			"VERSION": "98"
		}

def get_api_url():
	paypal_settings = get_paypal_settings()
	if paypal_settings.paypal_sandbox:
		return "https://api-3t.sandbox.paypal.com/nvp"
	else:
		return "https://api-3t.paypal.com/nvp"

def get_api_response(params):
	s = get_request_session()
	response = s.post(get_api_url(), data=params)
	response = urlparse.parse_qs(response.text)
	if response.get("ACK")[0]=="Success":
		return response
	else:
		raise PaypalException(response)

def get_paypal_settings():
	paypal_settings = frappe.get_doc("PayPal Settings")

	# update from site_config.json
	for key in ("paypal_sandbox", "paypal_username", "paypal_password", "paypal_signature"):
		if key in frappe.local.conf:
			paypal_settings.set(key, frappe.local.conf[key])

	return paypal_settings

def validate_transaction_currency(currency):
	if currency not in ["AUD", "BRL", "CAD", "CZK", "DKK", "EUR", "HKD", "HUF", "ILS", "JPY", "MYR", "MXN",
		"TWD", "NZD", "NOK", "PHP", "PLN", "GBP", "RUB", "SGD", "SEK", "CHF", "THB", "TRY", "USD"]:
		frappe.throw(_("Please select another payment method. PayPal not supports transaction currency {}".format(currency)))

def trigger_ref_doc(paypal_express_payment, method):
	page_mapper = {"Orders": "orders", "Invoices": "invoices", "My Account": "me"}
	if paypal_express_payment.reference_doctype and paypal_express_payment.reference_docname:

		ref_doc = frappe.get_doc(paypal_express_payment.reference_doctype,
			paypal_express_payment.reference_docname)
		ref_doc.run_method(method)

		if method != "set_as_cancelled":
			frappe.local.response["type"] = "redirect"
			shopping_cart_settings = frappe.get_doc("Shopping Cart Settings")
			if ref_doc.make_sales_invoice and shopping_cart_settings.enabled:
				success_url = shopping_cart_settings.payment_success_url
				if success_url:
					frappe.local.response["location"] = get_url("/{0}".format(page_mapper[success_url]))
				else:
					frappe.local.response["location"] = get_url("/orders/{0}".format(ref_doc.reference_name))
			else:
				frappe.local.response["location"] = get_url("/paypal-express-success")
