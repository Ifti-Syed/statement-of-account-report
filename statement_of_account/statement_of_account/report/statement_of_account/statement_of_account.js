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

// PDF download filename: "SOA_<Customer>_<As of date>.pdf" instead of Frappe's default
// "<report name>_<filter values>.pdf". Same scoped-override approach as above - only
// intercepts for Statement of Account, every other report keeps the stock filename logic.
(function () {
	if (frappe.views.QueryReport.prototype.__statement_of_account_pdf_patched) return;
	frappe.views.QueryReport.prototype.__statement_of_account_pdf_patched = true;

	const original_pdf_report = frappe.views.QueryReport.prototype.pdf_report;

	frappe.views.QueryReport.prototype.pdf_report = async function (print_settings) {
		if (this.report_name !== "Statement of Account") {
			return original_pdf_report.call(this, print_settings);
		}

		// Same body as the stock pdf_report() - this.report_name must stay untouched
		// throughout, since the print content's own title/heading reads it too; only the
		// final print_settings.report_name (the actual download filename) is computed
		// differently, right at the end.
		const base_url = frappe.urllib.get_base_url();
		const print_css = frappe.boot.print_css;
		const landscape = print_settings.orientation == "Landscape";

		let custom_format = this.report_settings.html_format || null;
		const columns = this.get_columns_for_print(print_settings, custom_format);
		const data = this.get_data_for_print();
		const applied_filters = this.get_filter_values();

		if (print_settings.report) {
			custom_format = await this.get_report_print_format(print_settings.report);
		}

		const filters_html = this.get_filters_html_for_print();
		const template = print_settings.columns || !custom_format ? "print_grid" : custom_format;
		const content = frappe.render_template(template, {
			title: __(this.report_name),
			subtitle: print_settings?.include_filters ? filters_html : null,
			filters: applied_filters,
			data: data,
			original_data: this.data,
			columns: columns,
			report: this,
		});

		const html = frappe.render_template("print_template", {
			title: __(this.report_name),
			content: content,
			base_url: base_url,
			print_css: print_css,
			print_settings: print_settings,
			landscape: landscape,
			columns: columns,
			lang: frappe.boot.lang,
			layout_direction: frappe.utils.is_rtl() ? "rtl" : "ltr",
			can_use_smaller_font: this.report_doc.is_standard === "Yes" && custom_format ? 0 : 1,
		});

		const safe = (s) => String(s).replace(/[\\/:*?"<>|]/g, "-");
		let customer_label = "All Customers";
		if (applied_filters.party) {
			customer_label = Array.isArray(applied_filters.party)
				? applied_filters.party.join("-")
				: applied_filters.party;
		}
		const date_label = applied_filters.report_date || frappe.datetime.get_today();
		print_settings.report_name = `SOA_${safe(customer_label)}_${safe(date_label)}.pdf`;

		frappe.render_pdf(html, print_settings);
	};
})();
