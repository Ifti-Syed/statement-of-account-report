frappe.provide("erpnext.utils");

frappe.query_reports["Statement of Account"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "report_date",
			label: __("Posting Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "party",
			label: __("Customer"),
			fieldtype: "MultiSelectList",
			options: "Customer",
			get_data: function (txt) {
				return frappe.db.get_link_options("Customer", txt);
			},
		},
		{
			fieldname: "ageing_based_on",
			label: __("Ageing Based On"),
			fieldtype: "Select",
			options: "Posting Date\nDue Date",
			default: "Posting Date",
		},
		{
			fieldname: "range",
			label: __("Ageing Range"),
			fieldtype: "Data",
			default: "30, 60, 90, 120",
		},
		{
			fieldname: "customer_group",
			label: __("Customer Group"),
			fieldtype: "MultiSelectList",
			options: "Customer Group",
			get_data: function (txt) {
				return frappe.db.get_link_options("Customer Group", txt);
			},
		},
		{
			fieldname: "report_type",
			label: __("Report Type"),
			fieldtype: "Select",
			options: "Projectwise\nWithout Project",
			default: "Projectwise",
		},
		{
			fieldname: "group_by_party",
			label: __("Group By Customer"),
			fieldtype: "Check",
		},
		{
			fieldname: "based_on_payment_terms",
			label: __("Based On Payment Terms"),
			fieldtype: "Check",
		},
		{
			fieldname: "show_delivery_notes",
			label: __("Show Linked Delivery Notes"),
			fieldtype: "Check",
		},
		{
			fieldname: "ignore_accounts",
			label: __("Group by Voucher"),
			fieldtype: "Check",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (data && data.bold) {
			value = value.bold();
		}
		return value;
	},
};

erpnext.utils.add_dimensions("Statement of Account", 5);

// The Print/PDF menu's "Print Settings" dialog (frappe.ui.get_print_settings) is shared by
// every report - there's no per-report flag to trim its fields. This wraps it so that only
// when Statement of Account is the active report, the dialog is reduced to just Orientation,
// with the letterhead applied automatically (using this report's own Company filter, not the
// user's global default company) instead of asking. Every other report gets the untouched
// original dialog, unaffected.
(function () {
	if (frappe.ui.__statement_of_account_print_settings_patched) return;
	frappe.ui.__statement_of_account_print_settings_patched = true;

	const original_get_print_settings = frappe.ui.get_print_settings;

	frappe.ui.get_print_settings = function (pdf, callback, letter_head, pick_columns, has_filters) {
		if (!frappe.query_report || frappe.query_report.report_name !== "Statement of Account") {
			return original_get_print_settings.apply(this, arguments);
		}

		const print_settings = locals[":Print Settings"]["Print Settings"];
		const company = frappe.query_report.get_filter_value("company");
		let default_letter_head = letter_head || "";
		if (!default_letter_head && company && locals[":Company"] && locals[":Company"][company]) {
			default_letter_head = locals[":Company"][company]["default_letter_head"] || "";
		}

		return frappe.prompt(
			[
				{
					fieldtype: "Select",
					fieldname: "orientation",
					label: __("Orientation"),
					options: [
						{ value: "Landscape", label: __("Landscape") },
						{ value: "Portrait", label: __("Portrait") },
					],
					default: "Landscape",
				},
			],
			function (dialog_data) {
				let data = $.extend(print_settings, dialog_data, {
					with_letter_head: 1,
					letter_head: default_letter_head,
				});
				data.letter_head = data.letter_head
					? frappe.boot.letter_heads[data.letter_head]
					: null;
				callback(data);
			},
			__("Print Settings")
		);
	};
})();
