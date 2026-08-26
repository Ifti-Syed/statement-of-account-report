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

	def append_row(self, row):
		super().append_row(row)
		row.project = row.get("project") or ""

	def get_data(self):
		super().get_data()
		self.append_unallocated_pdc_rows()

	def append_unallocated_pdc_rows(self):
		"""PDC cheques received and recorded (as a Payment Entry) but still sitting in the
		"Submitted for approval" workflow step, with money not yet allocated to any invoice.

		These never show up anywhere in the base AR data at all - they're outside the
		Payment Ledger Entry pipeline the rest of this report is built on. Critically, a
		Payment Entry only feeds that pipeline once it's actually accounting-submitted
		(docstatus=1) - "pending approval" by definition means docstatus=0, still Draft.
		Filtering on docstatus=1 (or <2, which also allows 1) here would double-count:
		once via its real, correctly-aged row from the base pipeline, and again via this
		flat, unaged row. So this is deliberately docstatus=0 only, with a same-name
		safety check against rows the base pipeline already added, just in case a
		submitted Payment Entry is ever left holding a stale "Submitted for approval"
		workflow_state value.
		"""
		filters = {
			"party_type": "Customer",
			"workflow_state": "Submitted for approval",
			"unallocated_amount": (">", 0),
			"docstatus": 0,
		}
		if self.filters.get("company"):
			filters["company"] = self.filters.company
		if self.filters.get("party"):
			filters["party"] = ("in", self.filters.party)

		pdc_entries = frappe.get_all(
			"Payment Entry",
			filters=filters,
			fields=["name", "party", "posting_date", "reference_date", "reference_no", "unallocated_amount"],
		)

		existing_voucher_nos = {row.voucher_no for row in self.data if row.get("voucher_no")}

		for pe in pdc_entries:
			if pe.name in existing_voucher_nos:
				continue
			amount = flt(pe.unallocated_amount, self.currency_precision)
			self.data.append(
				frappe._dict(
					party_type="Customer",
					party=pe.party,
					voucher_type="Payment Entry",
					voucher_no=pe.name,
					posting_date=pe.reference_date or pe.posting_date,
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
					future_ref=pe.reference_no,
					future_amount=amount,
					remaining_balance=0.0,
					project="",
					is_unallocated_pdc=1,
				)
			)

	def get_columns(self):
		super().get_columns()

		# Not needed on this report.
		drop_fields = {"customer_primary_contact", "cost_center", "territory", "customer_group"}
		self.columns = [c for c in self.columns if c["fieldname"] not in drop_fields]

		# Appended at the end, after the PDC/LC and Remaining Balance columns.
		self.columns.append(
			dict(label=_("Project"), fieldname="project", fieldtype="Link", options="Project", width=140)
		)

		for column in self.columns:
			if column["fieldname"] == "future_ref":
				column["label"] = _("PDC/LC Ref")
			elif column["fieldname"] == "future_amount":
				column["label"] = _("PDC/LC Amount")
