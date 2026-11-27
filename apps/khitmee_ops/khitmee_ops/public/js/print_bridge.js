(function () {
	"use strict";

	function companionPrint(payload, token, endpoint) {
		const controller = new AbortController();
		const timeout = window.setTimeout(function () {
			controller.abort();
		}, 2500);
		const headers = { "Content-Type": "application/json", Accept: "application/json" };
		if (token) headers["X-Khitmee-Print-Token"] = token;

		return fetch(endpoint || "http://127.0.0.1:18421/print", {
			method: "POST",
			mode: "cors",
			headers: headers,
			body: JSON.stringify(payload),
			signal: controller.signal,
		})
			.then(async function (response) {
				if (!response.ok) throw new Error("Print companion returned " + response.status);
				const body = await response.json().catch(function () {
					return {};
				});
				return {
					printed: !body.dry_run,
					dry_run: !!body.dry_run,
					transport: "companion",
				};
			})
			.finally(function () {
				window.clearTimeout(timeout);
			});
	}

	function textNode(tag, className, text) {
		const node = document.createElement(tag);
		if (className) node.className = className;
		node.textContent = text == null ? "" : String(text);
		return node;
	}

	function buildReceipt(payload) {
		const receipt = document.createElement("div");
		receipt.className = "print-receipt " + (payload.printer_type === "a4" ? "print-receipt-a4" : "print-receipt-voucher");
		receipt.appendChild(textNode("h2", "", payload.title || "Voucher"));
		receipt.appendChild(textNode("p", "receipt-shop", payload.shop_name || ""));
		receipt.appendChild(textNode("p", "receipt-meta", (payload.voucher_no || "") + " · " + (payload.date || "")));
		if (payload.party) receipt.appendChild(textNode("p", "receipt-party", payload.party));

		const table = document.createElement("table");
		const thead = document.createElement("thead");
		thead.innerHTML = "<tr><th>Item</th><th>Qty</th><th>Rate</th><th>Amount</th></tr>";
		table.appendChild(thead);
		const tbody = document.createElement("tbody");
		(payload.lines || []).forEach(function (line) {
			const tr = document.createElement("tr");
			[ line.name, line.qty, line.rate, line.amount ].forEach(function (value) {
				const td = document.createElement("td");
				td.textContent = value == null ? "" : String(value);
				tr.appendChild(td);
			});
			tbody.appendChild(tr);
		});
		table.appendChild(tbody);
		receipt.appendChild(table);
		receipt.appendChild(textNode("p", "receipt-total", "Total: " + (payload.total || 0)));
		if (payload.footer) receipt.appendChild(textNode("p", "receipt-footer", payload.footer));
		return receipt;
	}

	function browserPrintFallback(payload, options) {
		const opts = Object.assign({ autoPrint: true, notice: "" }, options || {});
		let dialog = document.getElementById("print-fallback-dialog");
		if (!dialog) {
			dialog = document.createElement("dialog");
			dialog.id = "print-fallback-dialog";
			dialog.className = "print-fallback-dialog";
			document.body.appendChild(dialog);
		}
		dialog.innerHTML = "";
		const actions = document.createElement("div");
		actions.className = "print-fallback-actions no-print";
		if (opts.notice) {
			const notice = textNode("span", "status", opts.notice);
			notice.dataset.kind = "error";
			notice.style.marginInlineEnd = "auto";
			actions.appendChild(notice);
		}
		const printButton = document.createElement("button");
		printButton.type = "button";
		printButton.textContent = opts.autoPrint === false ? "Reprint via Browser" : "Print";
		const closeButton = document.createElement("button");
		closeButton.type = "button";
		closeButton.textContent = "Close";
		actions.appendChild(printButton);
		actions.appendChild(closeButton);
		dialog.appendChild(actions);
		dialog.appendChild(buildReceipt(payload));

		printButton.addEventListener("click", function () {
			window.print();
		});
		closeButton.addEventListener("click", function () {
			dialog.close();
		});
		dialog.addEventListener("close", function () {
			document.body.classList.remove("print-fallback-open");
		});

		document.body.classList.add("print-fallback-open");
		if (typeof dialog.showModal === "function") dialog.showModal();
		else dialog.setAttribute("open", "open");
		if (opts.autoPrint !== false) {
			window.setTimeout(function () {
				window.print();
			}, 50);
			return Promise.resolve({ printed: true, transport: "browser" });
		}
		return Promise.resolve({ printed: false, transport: "uncertain" });
	}

	function printReceipt(payload, token, endpoint) {
		return companionPrint(payload, token, endpoint).catch(function (error) {
			if (error && error.name === "AbortError") {
				return browserPrintFallback(payload, {
					autoPrint: false,
					notice: "Print companion timed out — check printer before clicking Print to avoid duplicates.",
				});
			}
			return browserPrintFallback(payload, { autoPrint: true });
		});
	}

	window.KhitmeePrintBridge = { printReceipt: printReceipt };
})();
