# Experiment 0001 — native Rust framework selection

Date: 2026-11-27

## Decision under test

Choose the first implementation framework for progressively recreating Khitmee ERP web screens as a native Rust desktop application without Tauri, Electron, WebView, or an embedded browser.

## Candidates

| Framework | Native rendering | Headless/agent inspection | Accessibility evidence | ERP fit | Risks |
|---|---|---|---|---|---|
| Slint 1.19.1 | Declarative retained-mode UI rendered by Slint backends; no browser engine | Embedded MCP server can list windows, inspect accessibility/properties/geometry, click, type, scroll and capture PNGs; headless testing backend exists | Accessibility properties exposed through introspection | Strong component/model separation, virtualized `ListView`, desktop/mobile support | License requires visible `AboutSlint` attribution or alternative license; MCP/testing APIs are still evolving |
| GPUI | GPU-accelerated Rust UI used by Zed; no browser engine | `TestApp` supports deterministic headless tests; no built-in MCP server found | AccessKit tree support and testing APIs | Powerful for custom editor-like UI | Pre-1.0 API churn; application code is more imperative; fewer declarative ERP form/table primitives |
| Iced | Native Rust retained-ish widget model; no browser engine | `iced_test` supports headless simulation and snapshot-oriented tests; no built-in MCP server found | Depends on integration; testing story is younger | Good Elm-style state separation | Large virtualized ERP grids and accessibility evidence require more custom work |
| egui | Immediate-mode native Rust UI; no browser engine | `egui_kittest` supports AccessKit queries and image snapshots; no built-in MCP server found | AccessKit support when enabled | Fast iteration for tools | Immediate-mode allocation/render behavior and complex ERP form ownership need careful architecture; styling parity is custom |

## Selection

Slint is the initial framework. The deciding evidence is not aesthetics; it is verification infrastructure:

1. The built-in testing backend exposes an MCP server when compiled with `slint/mcp` and started with `SLINT_MCP_PORT`.
2. The MCP server exposes machine-readable windows, element trees, geometry, accessibility properties, keyboard/pointer events, scrolling and PNG screenshots.
3. `ListView` instantiates only visible rows, which is directly relevant to 10,000 and 100,000-row ERP list qualification.
4. Declarative `.slint` files give shared components stable ownership boundaries, reducing merge conflicts between independent migration agents.
5. Slint does not embed Chromium/WebView/Tauri/Electron.

## Licensing requirement

The pilot uses Slint's royalty-free desktop license path and therefore includes an accessible About screen containing the `AboutSlint` attribution widget. Embedded appliance use would require separate licensing review; this project targets general-purpose Windows cashier PCs.

## Representative workload gates

A framework is not qualified for mass migration until all of these pass against identical fixtures:

- 10,000 and 100,000-row virtualized list rendering and scrolling.
- Burmese Unicode shaping and font fallback.
- Exact barcode scan input, keyboard-first operation, and F2/F9 parity.
- Cold startup, idle/peak memory, CPU, frame latency, input latency and allocation measurements.
- Screenshot/layout/text/accessibility comparison against deterministic ERPNext web references.
- Business outcome comparison against authoritative ERPNext for submission fixtures.
- Print path investigation and measurement.

## Current result

Slint is **provisionally selected for pilot implementation only**. Mass migration remains blocked until `0002-qualification-ledger.md` contains passing evidence for the pilot screens. No visual, behavioral or performance parity is claimed in this experiment document.
