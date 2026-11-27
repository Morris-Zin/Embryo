(function () {
	"use strict";

	window.addEventListener("DOMContentLoaded", function () {
		window.KhitmeeCounterCore.createCounter({
			mode: "transfer",
			showRates: false,
			submitEndpoint: "khitmee_ops.api.vouchers.submit_transfer",
			searchWarehouse: function (els) {
				return els.warehouse.value;
			},
			validate: function (els) {
				if (!els.warehouse.value) throw new Error("Choose a source warehouse.");
				if (!els.targetWarehouse.value) throw new Error("Choose a target warehouse.");
				if (els.warehouse.value === els.targetWarehouse.value) {
					throw new Error("Source and target warehouses must be different.");
				}
				if (els.transferMode.value === "receive" && !els.outgoingStockEntry.value.trim()) {
					throw new Error("Enter the outgoing stock entry to receive.");
				}
				return {
					source_warehouse: els.warehouse.value,
					target_warehouse: els.targetWarehouse.value,
					transfer_mode: els.transferMode.value,
					outgoing_stock_entry: els.outgoingStockEntry.value.trim() || null,
				};
			},
		});
	});
})();
