(function () {
	"use strict";

	window.addEventListener("DOMContentLoaded", function () {
		window.KhitmeeCounterCore.createCounter({
			mode: "sales",
			showRates: true,
			submitEndpoint: "khitmee_ops.api.vouchers.submit_sale",
			searchWarehouse: function (els) {
				return els.warehouse.value;
			},
			validate: function (els) {
				if (!els.warehouse.value) throw new Error("Choose a shop warehouse.");
				if (!els.customer.value) throw new Error("Choose a customer.");
				if (!els.priceList.value) throw new Error("Price list is not configured.");
				return {
					warehouse: els.warehouse.value,
					customer: els.customer.value,
					price_list: els.priceList.value,
					payment_mode: els.paymentMode.value || null,
				};
			},
		});
	});
})();
