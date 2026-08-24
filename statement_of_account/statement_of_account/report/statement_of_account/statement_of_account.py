import frappe
from frappe import _
from frappe.utils import flt

from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

ARGS = {
	"account_type": "Receivable",
	"naming_by": ["Selling Settings", "cust_master_name"],
}


def execute(filters=None):
	return StatementOfAccount(filters).run(ARGS)


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

	def get_future_payments(self):
		# Same two sources as the base report, but also totals PDC/LC entries that
		# carry no invoice_no at all (advances not allocated to any voucher) - the
		# base report silently drops these; this report surfaces them separately as
		# "Unallocated PDC/LC Amount" in the Project Wise Summary instead of hiding them.
		self.future_payments = frappe._dict()
		self.unallocated_pdc_amount = 0.0

		if not self.filters.show_future_payments:
			return

		future_payments = list(self.get_future_payments_from_payment_entry())
		future_payments += list(self.get_future_payments_from_journal_entry())

		future_amount_field = (
			"future_amount" if self.filters.in_party_currency else "future_amount_in_base_currency"
		)

		for d in future_payments:
			if not d.future_amount:
				continue
			if d.invoice_no:
				self.future_payments.setdefault((d.invoice_no, d.party), []).append(d)
			else:
				self.unallocated_pdc_amount += flt(d.get(future_amount_field))

	def append_row(self, row):
		super().append_row(row)
		row.project = row.get("project") or ""
		# Stashed on every row (not just a totals row) so it survives into the
		# print template's `data` array regardless of which rows are visible/sorted.
		row.unallocated_pdc_amount = flt(self.unallocated_pdc_amount, self.currency_precision)

	def get_columns(self):
		super().get_columns()

		voucher_no_idx = next(i for i, c in enumerate(self.columns) if c["fieldname"] == "voucher_no")
		self.columns.insert(
			voucher_no_idx + 1,
			dict(label=_("Project"), fieldname="project", fieldtype="Link", options="Project", width=140),
		)

		for column in self.columns:
			if column["fieldname"] == "future_ref":
				column["label"] = _("PDC/LC Ref")
			elif column["fieldname"] == "future_amount":
				column["label"] = _("PDC/LC Amount")
