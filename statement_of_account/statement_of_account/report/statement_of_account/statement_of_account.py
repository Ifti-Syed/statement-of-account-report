import frappe
from frappe import _
from frappe.query_builder.functions import IfNull, Sum
from frappe.utils import flt, fmt_money, get_link_to_form

from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

ARGS = {
	"account_type": "Receivable",
	"naming_by": ["Selling Settings", "cust_master_name"],
}

# Workflow states that represent a PDC record that isn't a real cheque actually held (e.g. a
# scanned copy kept on file, or a bank guarantee cheque never meant to be presented) - excluded
# from Unallocated PDC/LC the same way they're excluded from PDC/LC in Hand.
EXCLUDED_PDC_WORKFLOW_STATES = ["Cheque copy", "Guarantee Cheque"]


def execute(filters=None):
	report = StatementOfAccount(filters)
	columns, data, message, chart, report_summary, skip_total_row = report.run(ARGS)
	if report.pdc_warnings:
		message = "<br>".join(report.pdc_warnings)
	return columns, data, message, chart, report_summary, skip_total_row


class StatementOfAccount(ReceivablePayableReport):
	"""Accounts Receivable, relabelled/extended for customer-facing statements.

	Reuses ReceivablePayableReport's invoice/ageing/PDC allocation logic as-is
	(same outstanding, ageing bucket and future-payment numbers as the standard
	Accounts Receivable report); only adds a Project column, renames the PDC/LC
	related labels, and tracks unallocated PDC/LC amount for the Project Wise
	Summary printed at the bottom of this report.
	"""

	def set_defaults(self):
		# PDC/LC allocation is core to this report, always compute it - the
		# corresponding filter is not exposed in the UI (see statement_of_account.js).
		self.filters.show_future_payments = 1
		# "Calculate Ageing With" is not exposed as a filter on this report - ageing is
		# always calculated against today's date.
		self.filters.calculate_ageing_with = "Today Date"
		super().set_defaults()

	def get_invoice_details(self):
		super().get_invoice_details()
		self.set_project_details()

	def set_project_details(self):
		si_list = frappe.get_all(
			"Sales Invoice",
			filters={
				"posting_date": ("<=", self.filters.report_date),
				"company": self.filters.company,
				"docstatus": 1,
			},
			fields=["name", "project"],
		)
		for d in si_list:
			if d.project:
				self.invoice_details.setdefault(d.name, {})["project"] = d.project

		# Journal Entry Account carries its own project per accounting line; a Journal
		# Entry can in principle post to more than one project, but this report (like
		# the base invoice_details map it extends) tracks one project per voucher, so
		# we take the first non-null project found per parent.
		# nosemgrep
		je_projects = frappe.db.sql(
			"""
			select jea.parent, jea.project
			from `tabJournal Entry Account` jea
			inner join `tabJournal Entry` je on je.name = jea.parent
			where je.docstatus = 1
				and je.posting_date <= %(report_date)s
				and je.company = %(company)s
				and jea.project is not null and jea.project != ''
			group by jea.parent
			""",
			{"report_date": self.filters.report_date, "company": self.filters.company},
			as_dict=1,
		)
		for d in je_projects:
			self.invoice_details.setdefault(d.parent, {})["project"] = d.project

	def append_row(self, row):
		super().append_row(row)
		row.project = row.get("project") or ""

	def get_data(self):
		self.pdc_warnings = []
		super().get_data()
		self.append_unallocated_pdc_rows()

	def append_unallocated_pdc_rows(self):
		"""Unallocated PDC/LC = Payment Entry Paid Amount - Sales-Invoice-allocated Amount.

		Deliberately NOT Payment Entry.unallocated_amount - that field also nets out any
		allocation against a Sales Order, which this report must not do (an SO allocation
		doesn't reduce what's still unapplied *against an invoice*).

		Same business conditions as the base report's own PDC/LC in Hand query
		(get_future_payments_from_payment_entry): Customer, not cancelled, posting_date
		after the report date. Additionally excludes the two workflow states that don't
		represent a real cheque in hand.

		Avoiding double counting without losing Sales-Order-allocated money: a submitted
		Payment Entry that still has a leftover balance already appears in the base AR
		pipeline as its own standalone row (voucher_type=Payment Entry, voucher_no=this
		PE's name), with that leftover as its `outstanding`. That row only ever reflects
		money the Payment Ledger actually recognizes as unapplied - a Sales Order
		allocation isn't a voucher the AR ledger tracks, so it's invisible there even
		though it's still not applied to any invoice. So rather than skip the whole
		Payment Entry whenever it already has a row (which would silently drop that SO
		portion), this looks up whatever the base pipeline already shows for this exact
		Payment Entry and only adds the difference between that and this method's own
		Paid Amount - Sales Invoice Allocated figure. When there's no existing row (the
		common "pending approval" case), the difference is simply the full amount.
		"""
		pe = frappe.qb.DocType("Payment Entry")
		per = frappe.qb.DocType("Payment Entry Reference")

		query = (
			frappe.qb.from_(pe)
			.left_join(per)
			.on((per.parent == pe.name) & (per.reference_doctype == "Sales Invoice"))
			.select(
				pe.name,
				pe.party,
				pe.posting_date,
				pe.reference_date,
				pe.reference_no,
				pe.base_paid_amount,
				pe.source_exchange_rate,
				IfNull(Sum(per.allocated_amount), 0).as_("si_allocated_amount"),
			)
			.where(
				(pe.party_type == "Customer")
				& (pe.docstatus < 2)
				& (pe.posting_date > self.filters.report_date)
				& (IfNull(pe.workflow_state, "").notin(EXCLUDED_PDC_WORKFLOW_STATES))
			)
			.groupby(pe.name)
		)
		if self.filters.get("company"):
			query = query.where(pe.company == self.filters.company)
		if self.filters.get("party"):
			query = query.where(pe.party.isin(self.filters.party))

		pdc_entries = query.run(as_dict=True)

		existing_by_voucher_no = {row.voucher_no: row for row in self.data if row.get("voucher_no")}

		for entry in pdc_entries:
			exchange_rate = flt(entry.source_exchange_rate) or 1.0
			si_allocated_base = flt(entry.si_allocated_amount) * exchange_rate
			unallocated = flt(flt(entry.base_paid_amount) - si_allocated_base, self.currency_precision)

			if unallocated < 0:
				# Sales Invoice allocation exceeds what was actually paid - this is bad
				# data on the Payment Entry itself. Never show a negative PDC/LC figure;
				# flag it instead of silently absorbing the discrepancy. Note this only
				# skips this entry's contribution to the *Unallocated* PDC/LC figure -
				# its Sales-Invoice-allocated amount is unaffected and still shows
				# normally in the PDC/LC Amount column against that invoice, via the
				# regular Accounts Receivable logic this report doesn't touch.
				self.pdc_warnings.append(
					'<span style="color:red;font-weight:bold;">'
					+ _(
						"Warning: Payment Entry {0} (Customer: {1}) has a Sales Invoice "
						"allocated amount ({2}) greater than its Paid Amount ({3}). This is "
						"invalid data on the Payment Entry - please correct it. "
					).format(
						get_link_to_form("Payment Entry", entry.name),
						entry.party,
						fmt_money(si_allocated_base, currency=self.company_currency),
						fmt_money(entry.base_paid_amount, currency=self.company_currency),
					)
					+ "</span>"
				)
				continue

			# Whatever the base AR pipeline already shows for this exact Payment Entry
			# (its own standalone row's outstanding, if one exists) has already been
			# counted once, in the main table's Total Outstanding. Only the remainder -
			# money this method's own formula finds but the base pipeline doesn't show
			# anywhere (Sales-Order-allocated amounts, being the main case) - is new.
			existing_row = existing_by_voucher_no.get(entry.name)
			already_represented = abs(flt(existing_row.get("outstanding"))) if existing_row else 0.0
			missing = flt(unallocated - already_represented, self.currency_precision)

			if missing <= 0:
				continue

			self.data.append(
				frappe._dict(
					party_type="Customer",
					party=entry.party,
					voucher_type="Payment Entry",
					voucher_no=entry.name,
					posting_date=entry.reference_date or entry.posting_date,
					currency=self.company_currency,
					invoiced=0.0,
					paid=0.0,
					credit_note=0.0,
					outstanding=0.0,
					age="",
					range1=0.0,
					range2=0.0,
					range3=0.0,
					range4=0.0,
					range5=0.0,
					future_ref=entry.reference_no,
					future_amount=missing,
					# outstanding is 0 for these rows, so this keeps the base report's own
					# per-row identity (outstanding = future_amount + remaining_balance)
					# holding true here too: 0 = missing + (-missing). That's what lets the
					# Project Wise Summary fold this into Remaining Balance without a
					# special case.
					remaining_balance=-missing,
					project="",
					is_unallocated_pdc=1,
				)
			)

	def get_columns(self):
		super().get_columns()

		# Not needed on this report.
		drop_fields = {
			"customer_primary_contact",
			"cost_center",
			"territory",
			"customer_group",
			"party_account",
		}
		self.columns = [c for c in self.columns if c["fieldname"] not in drop_fields]

		# No column definition inserted for "project" here on purpose - the on-screen
		# grid for this report already shows a Project column via a saved per-user
		# customization on the Frappe Cloud site, and adding a second definition here
		# produced a duplicate on screen. The underlying data (row.project, set by
		# append_row()/set_project_details() below) is untouched and still drives the
		# print template's Project column and the Project Wise Summary exactly as
		# before - only this report's own on-screen column definition is removed.
		for column in self.columns:
			if column["fieldname"] == "future_ref":
				column["label"] = _("PDC/LC Ref")
			elif column["fieldname"] == "future_amount":
				column["label"] = _("PDC/LC Amount")
