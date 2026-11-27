(function () {
	"use strict";

	const DEFAULT_SERVICE_PORT = 18422;
	const DB_NAME = "khitmee-catalog-poc";
	const STORE_NAME = "catalogs";
	const DEFAULT_STALE_AFTER_SECONDS = 120;
	const memoryRecords = new Map();
	const inflightSyncs = new Map();
	const preparedScopes = new Map();

	const status = {
		mode: "off",
		state: "off",
		scope: "",
		itemCount: 0,
		lastSyncMs: 0,
		lastSyncDurationMs: 0,
		lastError: "",
	};
	window.__khitmeeCatalogStatus = status;

	function serviceUrl() {
		// The port must never come from a navigable URL: a malicious counter link
		// could otherwise redirect catalog synchronization to an attacker listener.
		const raw = window.localStorage.getItem("khitmee-catalog-port") || String(DEFAULT_SERVICE_PORT);
		const port = Number(raw);
		if (!Number.isInteger(port) || port < 1 || port > 65535) return "http://127.0.0.1:" + DEFAULT_SERVICE_PORT;
		return "http://127.0.0.1:" + port;
	}

	function requestedMode() {
		const params = new URLSearchParams(window.location.search);
		const mode = (params.get("khitmee_catalog") || window.localStorage.getItem("khitmee-catalog-mode") || "").toLowerCase();
		return mode === "rust" || mode === "browser" ? mode : "off";
	}

	const mode = requestedMode();
	status.mode = mode;

	function updateStatus(patch) {
		Object.keys(patch || {}).forEach(function (key) {
			status[key] = patch[key];
		});
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
			if (args[key] !== undefined && args[key] !== null) params.set(key, args[key]);
		});
		return requestJson("/api/method/" + method + "?" + params.toString(), {
			method: "GET",
			credentials: "same-origin",
			headers: { Accept: "application/json" },
		});
	}

	function normalizeText(value) {
		return String(value || "").toLocaleLowerCase();
	}

	function scopeKey(scope) {
		const erpUser = window.frappe && ((frappe.session && frappe.session.user) || (frappe.boot && frappe.boot.user && frappe.boot.user.name)) || "unknown-user";
		return [window.location.origin, erpUser, scope.warehouse || "", scope.priceList || ""].join("\u001f");
	}

	function openDatabase() {
		if (!window.indexedDB) return Promise.reject(new Error("IndexedDB is unavailable"));
		return new Promise(function (resolve, reject) {
			const request = window.indexedDB.open(DB_NAME, 1);
			request.onupgradeneeded = function () {
				request.result.createObjectStore(STORE_NAME);
			};
			request.onsuccess = function () {
				resolve(request.result);
			};
			request.onerror = function () {
				reject(request.error || new Error("IndexedDB open failed"));
			};
		});
	}

	async function idbGet(key) {
		try {
			const db = await openDatabase();
			return await new Promise(function (resolve, reject) {
				const tx = db.transaction(STORE_NAME, "readonly");
				const request = tx.objectStore(STORE_NAME).get(key);
				request.onsuccess = function () {
					resolve(request.result || null);
				};
				request.onerror = function () {
					reject(request.error || new Error("IndexedDB read failed"));
				};
			});
		} catch (error) {
			return memoryRecords.get(key) || null;
		}
	}

	async function idbPut(key, value) {
		try {
			const db = await openDatabase();
			await new Promise(function (resolve, reject) {
				const tx = db.transaction(STORE_NAME, "readwrite");
				tx.objectStore(STORE_NAME).put(value, key);
				tx.oncomplete = function () {
					resolve();
				};
				tx.onerror = function () {
					reject(tx.error || new Error("IndexedDB write failed"));
				};
			});
			memoryRecords.delete(key);
		} catch (error) {
			// The in-memory copy is a safe PoC fallback when IndexedDB is unavailable.
			memoryRecords.set(key, value);
		}
	}

	async function browserState(key) {
		const record = await idbGet(key);
		return {
			sync_token: record && record.syncToken ? record.syncToken : "",
			item_count: record && record.items ? Object.keys(record.items).length : 0,
		};
	}

	function serviceHeaders(token) {
		const headers = { Accept: "application/json" };
		if (token) headers["X-Khitmee-Desktop-Token"] = token;
		return headers;
	}

	async function rustState(key, token) {
		const result = await requestJson(serviceUrl() + "/state?scope=" + encodeURIComponent(key), {
			method: "GET",
			headers: serviceHeaders(token),
		});
		return result;
	}

	async function applyBrowserSync(key, payload) {
		const previous = (await idbGet(key)) || { items: {} };
		const items = payload.reset ? {} : Object.assign({}, previous.items || {});
		(payload.items || []).forEach(function (row) {
			if (!row || !row.item_code) return;
			if (row.deleted) {
				delete items[row.item_code];
			} else {
				items[row.item_code] = row;
			}
		});
		const record = {
			scope: key,
			syncToken: payload.sync_token || "",
			items: items,
			updatedAt: Date.now(),
		};
		await idbPut(key, record);
		return { item_count: Object.keys(items).length };
	}

	async function applyRustSync(key, payload, token) {
		const headers = serviceHeaders(token);
		headers["Content-Type"] = "application/json";
		return requestJson(serviceUrl() + "/sync", {
			method: "POST",
			headers: headers,
			body: JSON.stringify({
				scope: key,
				reset: !!payload.reset,
				sync_token: payload.sync_token || "",
				items: payload.items || [],
			}),
		});
	}

	async function syncScope(scope) {
		const key = scopeKey(scope);
		const started = performance.now();
		updateStatus({ state: "syncing", scope: key, lastError: "" });
		try {
			const localState = mode === "rust" ? await rustState(key, scope.token) : await browserState(key);
			const payload = await apiGet("khitmee_ops.api.catalog.sync_catalog", {
				warehouse: scope.warehouse,
				price_list: scope.priceList,
				state: localState.sync_token || "",
			});
			if (payload.reset && payload.complete === false) {
				throw new Error("Catalog exceeds the local proof-of-concept item cap; using server search");
			}
			const applied = mode === "rust" ? await applyRustSync(key, payload, scope.token) : await applyBrowserSync(key, payload);
			const elapsed = performance.now() - started;
			preparedScopes.set(key, {
				at: Date.now(),
				staleAfterMs: Math.max(5, Number(payload.stale_after_seconds) || DEFAULT_STALE_AFTER_SECONDS) * 1000,
			});
			updateStatus({
				state: "ready",
				scope: key,
				itemCount: applied.item_count || 0,
				lastSyncMs: Date.now(),
				lastSyncDurationMs: Number(elapsed.toFixed(2)),
				lastError: "",
			});
			return true;
		} catch (error) {
			updateStatus({ state: "error", lastError: error.message || String(error) });
			return false;
		} finally {
			inflightSyncs.delete(key);
		}
	}

	function prepare(scope) {
		if (mode === "off" || !scope.warehouse || !scope.priceList) return Promise.resolve(false);
		const key = scopeKey(scope);
		const prepared = preparedScopes.get(key);
		if (prepared && Date.now() - prepared.at < prepared.staleAfterMs) return Promise.resolve(true);
		if (inflightSyncs.has(key)) return inflightSyncs.get(key);
		const promise = syncScope(scope);
		inflightSyncs.set(key, promise);
		return promise;
	}

	function rowMatchesBarcode(row, foldedTerm) {
		if ((row.barcode || "").toLocaleLowerCase() === foldedTerm) return true;
		return String(row.barcode_text || "")
			.toLocaleLowerCase()
			.split(/\s+/)
			.indexOf(foldedTerm) >= 0;
	}

	function rowSearchText(row) {
		return normalizeText([
			row.item_code,
			row.item_name,
			row.item_group,
			row.brand,
			row.barcode_text,
		].join("\u001f"));
	}

	function rankRows(itemMap, foldedTerm, limit) {
		const exact = [];
		const prefix = [];
		const substring = [];
		Object.keys(itemMap || {}).forEach(function (itemCode) {
			const row = itemMap[itemCode];
			const code = normalizeText(row.item_code);
			const name = normalizeText(row.item_name);
			const group = normalizeText(row.item_group);
			const brand = normalizeText(row.brand);
			if (rowMatchesBarcode(row, foldedTerm)) {
				exact.push(row);
			} else if (code.indexOf(foldedTerm) === 0 || name.indexOf(foldedTerm) === 0 || group.indexOf(foldedTerm) === 0 || brand.indexOf(foldedTerm) === 0) {
				prefix.push(row);
			} else if (rowSearchText(row).indexOf(foldedTerm) >= 0) {
				substring.push(row);
			}
		});
		if (exact.length) return exact.slice(0, limit);
		return prefix.concat(substring).slice(0, limit);
	}

	async function browserSearch(scope, term, limit) {
		const record = await idbGet(scopeKey(scope));
		if (!record || !record.items) return { items: [] };
		const foldedTerm = normalizeText(term).trim();
		if (!foldedTerm) return { items: [] };
		return { items: rankRows(record.items, foldedTerm, limit) };
	}

	async function rustSearch(scope, term, limit) {
		const params = new URLSearchParams({
			scope: scopeKey(scope),
			q: term,
			limit: String(limit),
		});
		return requestJson(serviceUrl() + "/search?" + params.toString(), {
			method: "GET",
			headers: serviceHeaders(scope.token),
		});
	}

	async function search(args) {
		if (mode === "off") return null;
		const scope = {
			warehouse: args.warehouse || "",
			priceList: args.priceList || "",
			token: args.token || "",
		};
		const ready = await prepare(scope);
		if (!ready) return null;
		try {
			if (mode === "rust") return await rustSearch(scope, args.term, args.limit || 10);
			return await browserSearch(scope, args.term, args.limit || 10);
		} catch (error) {
			updateStatus({ state: "error", lastError: error.message || String(error) });
			return null;
		}
	}

	window.KhitmeeCatalogBridge = {
		mode: function () {
			return mode;
		},
		prepare: prepare,
		search: search,
		status: status,
	};
})();
