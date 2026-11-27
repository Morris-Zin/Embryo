(function () {
	"use strict";

	function createDebouncer(fn, delay) {
		let timer = null;
		return {
			schedule: function () {
				const args = arguments;
				window.clearTimeout(timer);
				timer = window.setTimeout(function () {
					timer = null;
					fn.apply(null, args);
				}, delay);
			},
			cancel: function () {
				window.clearTimeout(timer);
				timer = null;
			},
		};
	}

	function formatMoney(value) {
		return new Intl.NumberFormat(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		}).format(Number(value || 0));
	}

	function formatQty(value) {
		return new Intl.NumberFormat(undefined, {
			minimumFractionDigits: 0,
			maximumFractionDigits: 3,
		}).format(Number(value || 0));
	}

	function uuid() {
		if (window.crypto && typeof window.crypto.randomUUID === "function") {
			return window.crypto.randomUUID();
		}
		return "voucher-" + Date.now() + "-" + Math.random().toString(16).slice(2);
	}

	async function requestJson(url, options) {
		const response = await fetch(url, options);
		const data = await response.json().catch(function () {
			return {};
		});
		if (!response.ok) {
			const message = data.exception || data.message || response.statusText || "Request failed";
			throw new Error(Array.isArray(message) ? message.join("\n") : message);
		}
		return data.message !== undefined ? data.message : data;
	}

	function apiGet(method, args) {
		const params = new URLSearchParams();
		Object.keys(args || {}).forEach(function (key) {
			if (args[key] !== undefined && args[key] !== null) {
				params.set(key, args[key]);
			}
		});
		return requestJson("/api/method/" + method + "?" + params.toString(), {
			method: "GET",
			credentials: "same-origin",
			headers: { Accept: "application/json" },
		});
	}

	function apiPost(method, args) {
		return requestJson("/api/method/" + method, {
			method: "POST",
			credentials: "same-origin",
			headers: {
				Accept: "application/json",
				"Content-Type": "application/json",
				"X-Frappe-CSRF-Token": window.csrf_token || "",
			},
			body: JSON.stringify(args || {}),
		});
	}

	function option(label, value) {
		const node = document.createElement("option");
		node.value = value;
		node.textContent = label;
		return node;
	}

	function createCounter(config) {
		const mode = config.mode || "sales";
		const els = {
			form: document.getElementById("counter-form"),
			warehouse: document.getElementById("warehouse"),
			targetWarehouse: document.getElementById("target-warehouse"),
			customer: document.getElementById("customer"),
			customerSearch: document.getElementById("customer-search"),
			customerMore: document.getElementById("customer-more"),
			paymentMode: document.getElementById("payment-mode"),
			priceList: document.getElementById("price-list"),
			transferMode: document.getElementById("transfer-mode"),
			outgoingStockEntry: document.getElementById("outgoing-stock-entry"),
			search: document.getElementById("item-search"),
			results: document.getElementById("search-results"),
			linesBody: document.getElementById("lines-body"),
			subtotal: document.getElementById("subtotal"),
			totalQty: document.getElementById("total-qty"),
			submit: document.getElementById("submit-print"),
			status: document.getElementById("status"),
			clear: document.getElementById("clear-lines"),
		};

		const state = {
			context: null,
			lines: [],
			searchItems: [],
			activeIndex: -1,
			clientVoucherId: uuid(),
			submitting: false,
			customerQuery: "",
			customerNextStart: 0,
			customersHaveMore: false,
		};

		function setStatus(message, kind) {
			els.status.textContent = message || "";
			els.status.dataset.kind = kind || "";
		}

		function printerType() {
			const checked = document.querySelector('input[name="printer_type"]:checked');
			return checked ? checked.value : "voucher";
		}

		function searchWarehouse() {
			return config.searchWarehouse(els);
		}

		function fillSelect(select, rows, labeler, value) {
			if (!select || !rows) return;
			select.innerHTML = "";
			rows.forEach(function (row) {
				select.appendChild(option(labeler(row), row.name));
			});
			if (value) select.value = value;
		}

		function prependBlank(select, label) {
			if (!select) return;
			const blank = option(label, "");
			select.insertBefore(blank, select.firstChild);
			select.value = "";
		}

		function refillCustomers(rows, append) {
			if (!els.customer) return;
			const selected = els.customer.value;
			if (!append) els.customer.innerHTML = "";
			rows.forEach(function (row) {
				els.customer.appendChild(option(row.customer_name || row.name, row.name));
			});
			if (selected) {
				if (!els.customer.querySelector('option[value="' + selected + '"]')) {
					els.customer.insertBefore(option(selected, selected), els.customer.firstChild);
				}
				els.customer.value = selected;
			}
			if (els.customerMore) els.customerMore.hidden = !state.customersHaveMore;
		}

		async function loadCustomers(query, start, append) {
			const result = await apiGet("khitmee_ops.api.search.search_customers", {
				query: query || null,
				start: start,
			});
			state.customerQuery = query || "";
			state.customerNextStart = result.next_start || 0;
			state.customersHaveMore = !!result.has_more;
			refillCustomers(result.customers || [], append);
		}

		const customerDebouncer = createDebouncer(function () {
			loadCustomers(els.customerSearch ? els.customerSearch.value.trim() : "", 0, false).catch(
				function (error) {
					setStatus(error.message, "error");
				}
			);
		}, 250);

		async function loadContext() {
			state.context = await apiGet("khitmee_ops.api.search.get_counter_context", { mode: mode });
			if (els.priceList) els.priceList.value = state.context.default_price_list || "";
			fillSelect(
				els.warehouse,
				state.context.warehouses,
				function (row) {
					return row.warehouse_name || row.name;
				},
				state.context.default_warehouse
			);
			fillSelect(
				els.targetWarehouse,
				state.context.warehouses,
				function (row) {
					return row.warehouse_name || row.name;
				},
				""
			);
			prependBlank(els.targetWarehouse, "Choose target warehouse");

			if (mode !== "transfer") {
				fillSelect(
					els.customer,
					state.context.customers,
					function (row) {
						return row.customer_name || row.name;
					},
					""
				);
				fillSelect(
					els.paymentMode,
					state.context.modes_of_payment,
					function (row) {
						return row.name;
					},
					""
				);
				prependBlank(els.customer, "Choose customer");
				prependBlank(els.paymentMode, "No payment / credit");
				state.customerNextStart = state.context.customers_next_start || 0;
				state.customersHaveMore = !!state.context.customers_have_more;
				if (els.customerMore) els.customerMore.hidden = !state.customersHaveMore;
			}
		}

		function hideResults() {
			state.searchItems = [];
			state.activeIndex = -1;
			els.results.innerHTML = "";
			els.results.hidden = true;
			els.search.setAttribute("aria-expanded", "false");
			els.search.removeAttribute("aria-activedescendant");
		}

		function renderResults() {
			els.results.innerHTML = "";
			if (!state.searchItems.length) {
				hideResults();
				return;
			}

			state.searchItems.forEach(function (item, index) {
				const li = document.createElement("li");
				li.id = "search-option-" + index;
				li.setAttribute("role", "option");
				li.setAttribute("aria-selected", index === state.activeIndex ? "true" : "false");
				li.className = "search-option" + (index === state.activeIndex ? " is-active" : "");

				const button = document.createElement("button");
				button.type = "button";
				button.className = "search-option-button";
				button.innerHTML =
					'<span class="search-item-main"><strong></strong><span></span></span>' +
					'<span class="search-item-meta"><span class="qty"></span><span class="rate"></span></span>';
				button.querySelector("strong").textContent = item.item_code;
				button.querySelector(".search-item-main span").textContent = item.item_name || "";
				button.querySelector(".qty").textContent = "Stock " + formatQty(item.actual_qty);
				button.querySelector(".rate").textContent = formatMoney(item.rate);
				button.addEventListener("click", function () {
					addItem(item);
				});
				li.appendChild(button);
				els.results.appendChild(li);
			});
			els.results.hidden = false;
			els.search.setAttribute("aria-expanded", "true");
		}

		async function searchItems() {
			const term = els.search.value.trim();
			if (!term || !searchWarehouse()) {
				hideResults();
				return;
			}
			try {
				const result = await apiGet("khitmee_ops.api.search.search_items", {
					q: term,
					warehouse: searchWarehouse(),
					price_list: state.context.default_price_list,
					customer: els.customer && els.customer.value ? els.customer.value : null,
					limit: 10,
				});
				if (els.search.value.trim() !== term) return;
				state.searchItems = result.items || [];
				state.activeIndex = state.searchItems.length ? 0 : -1;
				renderResults();
			} catch (error) {
				setStatus(error.message, "error");
			}
		}

		const searchDebouncer = createDebouncer(searchItems, 180);

		function addItem(item) {
			const existing = state.lines.find(function (line) {
				return line.item_code === item.item_code;
			});
			if (existing) {
				existing.qty += 1;
			} else {
				state.lines.push({
					item_code: item.item_code,
					item_name: item.item_name,
					uom: item.stock_uom,
					qty: 1,
					rate: Number(item.rate || 0),
					actual_qty: Number(item.actual_qty || 0),
				});
			}
			els.search.value = "";
			hideResults();
			renderLines();
			els.search.focus();
		}

		function removeLine(index) {
			state.lines.splice(index, 1);
			renderLines();
			els.search.focus();
		}

		function changeQty(index, delta) {
			const line = state.lines[index];
			if (!line) return;
			line.qty = Math.max(0, Number((line.qty + delta).toFixed(3)));
			if (line.qty === 0) {
				removeLine(index);
				return;
			}
			renderLines();
		}

		function setQty(index, qty) {
			const line = state.lines[index];
			if (!line) return;
			line.qty = Math.max(0, Number(qty) || 0);
			if (line.qty === 0) {
				removeLine(index);
				return;
			}
			renderTotals();
		}

		function lineTotal(line) {
			return line.qty * line.rate;
		}

		function renderLines() {
			els.linesBody.innerHTML = "";
			state.lines.forEach(function (line, index) {
				const tr = document.createElement("tr");
				tr.innerHTML =
					"<td class='line-item'><strong></strong><span></span></td>" +
					"<td class='line-qty'><div class='qty-stepper'>" +
					"<button type='button' data-action='dec' aria-label='Decrease quantity'>−</button>" +
					"<input inputmode='decimal' data-action='qty' aria-label='Quantity' />" +
					"<button type='button' data-action='inc' aria-label='Increase quantity'>+</button>" +
					"</div></td>" +
					"<td class='line-uom'></td>" +
					"<td class='line-rate numeric'></td>" +
					"<td class='line-amount numeric'></td>" +
					"<td class='line-actions'><button type='button' class='icon-button' data-action='remove' aria-label='Remove line'>×</button></td>";

				tr.querySelector("strong").textContent = line.item_code;
				tr.querySelector(".line-item span").textContent = line.item_name || "";
				tr.querySelector("input").value = line.qty;
				tr.querySelector(".line-uom").textContent = line.uom || "";
				tr.querySelector(".line-rate").textContent = config.showRates ? formatMoney(line.rate) : "—";
				tr.querySelector(".line-amount").textContent = config.showRates ? formatMoney(lineTotal(line)) : "—";
				tr.dataset.index = index;
				els.linesBody.appendChild(tr);
			});
			renderTotals();
		}

		function renderTotals() {
			const totalQty = state.lines.reduce(function (sum, line) {
				return sum + line.qty;
			}, 0);
			const subtotal = state.lines.reduce(function (sum, line) {
				return sum + lineTotal(line);
			}, 0);
			els.totalQty.textContent = formatQty(totalQty);
			els.subtotal.textContent = config.showRates ? formatMoney(subtotal) : formatQty(totalQty);
			els.submit.disabled = !state.lines.length || state.submitting;
		}

		function resetLines() {
			state.lines = [];
			state.clientVoucherId = uuid();
			renderLines();
		}

		function validateForm() {
			if (!state.lines.length) {
				throw new Error("Add at least one item before submitting.");
			}
			return config.validate(els);
		}

		async function submit() {
			if (state.submitting) return;
			try {
				const extra = validateForm();
				state.submitting = true;
				els.submit.disabled = true;
				setStatus("Submitting voucher…", "busy");

				const payload = Object.assign(
					{
						client_voucher_id: state.clientVoucherId,
						printer_type: printerType(),
						items: state.lines.map(function (line) {
							const item = {
								item_code: line.item_code,
								qty: line.qty,
								uom: line.uom,
							};
							if (config.showRates) item.rate = line.rate;
							return item;
						}),
					},
					extra
				);

				const result = await apiPost(config.submitEndpoint, payload);
				setStatus("Submitted " + result.voucher_no + ". Printing…", "busy");
				const printResult = await window.KhitmeePrintBridge.printReceipt(
					result.receipt_payload,
					state.context.print_token,
					state.context.print_endpoint
				);
				let printNote;
				if (printResult.dry_run) {
					printNote = " — print companion dry-run (nothing printed).";
				} else if (printResult.transport === "companion") {
					printNote = " and sent to printer.";
				} else if (printResult.transport === "browser") {
					printNote = " — browser print fallback opened.";
				} else {
					printNote = " — print status unclear; use the Reprint via Browser button.";
				}
				setStatus("Submitted " + result.voucher_no + printNote, "success");
				resetLines();
				els.search.focus();
			} catch (error) {
				setStatus(error.message, "error");
			} finally {
				state.submitting = false;
				renderTotals();
			}
		}

		els.search.addEventListener("input", function () {
			// Any typed change invalidates the currently displayed results immediately;
			// a fresh debounced search repopulates them.
			hideResults();
			searchDebouncer.schedule();
		});
		els.search.addEventListener("keydown", function (event) {
			if (event.key === "ArrowDown" || event.key === "ArrowUp") {
				if (!state.searchItems.length) return;
				event.preventDefault();
				const delta = event.key === "ArrowDown" ? 1 : -1;
				state.activeIndex = (state.activeIndex + delta + state.searchItems.length) % state.searchItems.length;
				els.search.setAttribute("aria-activedescendant", "search-option-" + state.activeIndex);
				renderResults();
			} else if (event.key === "Enter") {
				event.preventDefault();
				// Hardware scanners type and hit Enter faster than the debounce; cancel
				// any pending search and always run a fresh one for the current input.
				searchDebouncer.cancel();
				searchItems().then(function () {
					const item = state.searchItems[state.activeIndex] || state.searchItems[0];
					if (item) addItem(item);
				});
			} else if (event.key === "Escape") {
				hideResults();
			}
		});

		if (els.customerSearch) {
			els.customerSearch.addEventListener("input", function () {
				customerDebouncer.schedule();
			});
		}
		if (els.customerMore) {
			els.customerMore.addEventListener("click", function () {
				loadCustomers(state.customerQuery, state.customerNextStart, true).catch(function (error) {
					setStatus(error.message, "error");
				});
			});
		}

		els.linesBody.addEventListener("click", function (event) {
			const button = event.target.closest("button[data-action]");
			if (!button) return;
			const row = button.closest("tr");
			const index = Number(row.dataset.index);
			if (button.dataset.action === "inc") changeQty(index, 1);
			if (button.dataset.action === "dec") changeQty(index, -1);
			if (button.dataset.action === "remove") removeLine(index);
		});
		els.linesBody.addEventListener("change", function (event) {
			if (!event.target.matches('input[data-action="qty"]')) return;
			const row = event.target.closest("tr");
			setQty(Number(row.dataset.index), event.target.value);
		});
		els.form.addEventListener("submit", function (event) {
			event.preventDefault();
			submit();
		});
		els.clear.addEventListener("click", function () {
			resetLines();
			els.search.focus();
		});
		document.addEventListener("keydown", function (event) {
			if (event.key === "F2") {
				event.preventDefault();
				els.search.focus();
				els.search.select();
			} else if (event.key === "F9") {
				event.preventDefault();
				submit();
			}
		});
		if (els.transferMode) {
			els.transferMode.addEventListener("change", function () {
				const receive = els.transferMode.value === "receive";
				els.outgoingStockEntry.disabled = !receive;
				els.outgoingStockEntry.required = receive;
			});
		}

		loadContext()
			.then(function () {
				renderLines();
				els.search.focus();
				setStatus("Ready. Scan a barcode or press F2 to search.", "success");
			})
			.catch(function (error) {
				setStatus(error.message, "error");
			});
	}

	window.KhitmeeCounterCore = {
		createCounter: createCounter,
		formatMoney: formatMoney,
		formatQty: formatQty,
	};
})();
