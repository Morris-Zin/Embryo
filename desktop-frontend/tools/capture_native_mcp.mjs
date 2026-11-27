#!/usr/bin/env node
/* Capture native Slint evidence through the embedded MCP testing server. */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

function arg(name, fallback = '') {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

const port = Number(arg('port', process.env.SLINT_MCP_PORT || '49272'));
const screenId = arg('screen', '');
if (!screenId) throw new Error('--screen is required so evidence cannot be filed under the wrong screen');
const stateName = arg('state', 'initial');
if (!/^[a-z0-9][a-z0-9-]*$/.test(stateName)) throw new Error('state must be a lowercase evidence directory name');
const defaultOut = stateName === 'initial' ? path.join('desktop-frontend/evidence/native', screenId) : path.join('desktop-frontend/evidence/native', screenId, stateName);
const outDir = path.resolve(arg('out', defaultOut));
const actions = JSON.parse(arg('actions', '[]'));
const endpoint = `http://127.0.0.1:${port}/mcp`;
let nextId = 1;

async function rpc(method, params = {}) {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
    body: JSON.stringify({ jsonrpc: '2.0', id: nextId++, method, params }),
  });
  if (!response.ok) throw new Error(`MCP ${method} HTTP ${response.status}: ${await response.text()}`);
  const text = await response.text();
  const dataText = text.startsWith('event:') || text.startsWith('data:')
    ? text.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
    : text;
  const parsed = JSON.parse(dataText);
  if (parsed.error) throw new Error(`MCP ${method}: ${JSON.stringify(parsed.error)}`);
  return parsed.result;
}

async function callTool(name, args = {}) {
  const result = await rpc('tools/call', { name, arguments: args });
  if (result.isError) throw new Error(`${name} failed: ${JSON.stringify(result)}`);
  return result;
}

function structured(result) {
  if (result.structuredContent) return result.structuredContent;
  const text = result.content?.find((item) => item.type === 'text')?.text;
  if (!text) return result;
  try { return JSON.parse(text); } catch { return { text }; }
}


const specialKeys = Object.fromEntries(Object.entries({ Enter: 10, Escape: 27, Tab: 9, Backspace: 8, F1: 0xF704, F2: 0xF705, F3: 0xF706, F4: 0xF707, F5: 0xF708, F6: 0xF709, F7: 0xF70A, F8: 0xF70B, F9: 0xF70C, F10: 0xF70D, F11: 0xF70E, F12: 0xF70F }).map(([key, code]) => [key, String.fromCharCode(code)]));
function keyText(key) { return specialKeys[key] || key; }
async function getTree(rootHandle) {
  return structured(await callTool('get_element_tree', { elementHandle: rootHandle, maxElements: 1000 }));
}
function matchesElement(element, action) {
  if (action.elementId) return (element.typeNamesAndIds || []).some((entry) => (typeof entry === 'string' ? entry === action.elementId || entry.endsWith(`::${action.elementId}`) : entry.id === action.elementId));
  if (action.label) return element.accessibleLabel === action.label || element.accessibleValue === action.label;
  return false;
}
async function findTarget(rootHandle, action) {
  const tree = await getTree(rootHandle);
  const elements = tree.elements || [];
  const found = elements.find((element) => matchesElement(element, action));
  if (!found?.handle) throw new Error(`native target not found for action ${JSON.stringify(action)}`);
  return found;
}

async function main() {
  await fs.mkdir(outDir, { recursive: true });
  const initialized = await rpc('initialize', {
    protocolVersion: '2025-06-18',
    capabilities: {},
    clientInfo: { name: 'khitmee-native-verifier', version: '0.1.0' },
  });
  await rpc('notifications/initialized', {}).catch(() => {});
  const tools = await rpc('tools/list', {});
  const windowsResult = structured(await callTool('list_windows'));
  const windows = windowsResult.windows || windowsResult.windowHandles || windowsResult.handles || windowsResult;
  const firstWindow = Array.isArray(windows) ? windows[0] : windows[0];
  if (!firstWindow) throw new Error(`No Slint windows in MCP response: ${JSON.stringify(windowsResult)}`);
  const windowHandle = firstWindow.windowHandle || firstWindow.handle || firstWindow;
  const properties = structured(await callTool('get_window_properties', { windowHandle }));
  const rootHandle = properties.rootElementHandle || properties.root_element_handle || properties.rootElement?.handle;
  if (!rootHandle) throw new Error(`No root element handle in ${JSON.stringify(properties)}`);
  const actionTrace = [];
  const keyboardResults = {};
  if (actions.length) await callTool('start_event_recording', { windowHandle });
  for (const [index, action] of actions.entries()) {
    actionTrace.push({ index, action });
    if (action.type === 'wait') await new Promise((resolve) => setTimeout(resolve, Number(action.ms || 0)));
    else if (action.type === 'key') await callTool('dispatch_key_event', { windowHandle, text: keyText(action.key), eventType: 'PressAndRelease' });
    else if (action.type === 'probe-key') {
      await callTool('dispatch_key_event', { windowHandle, text: keyText(action.key || action.shortcut), eventType: 'PressAndRelease' });
      await new Promise((resolve) => setTimeout(resolve, Number(action.focusSettleMs ?? 120)));
      if (action.probeText) {
        for (const character of String(action.probeText)) {
          await callTool('dispatch_key_event', { windowHandle, text: character, eventType: 'PressAndRelease' });
        }
        await new Promise((resolve) => setTimeout(resolve, Number(action.typingSettleMs ?? 120)));
      }
      const probeTree = await getTree(rootHandle);
      let passed = false;
      let evidence = {};
      if (action.targetElementId) {
        const target = (probeTree.elements || []).find((element) => matchesElement(element, { elementId: action.targetElementId }));
        evidence = { targetFound: Boolean(target), accessibleValue: target?.accessibleValue ?? null, probeText: action.probeText ?? '' };
        passed = Boolean(target) && String(target.accessibleValue || '').includes(String(action.probeText || ''));
      } else if (action.expectText) {
        evidence = { expectText: action.expectText };
        passed = JSON.stringify(probeTree).includes(String(action.expectText));
      }
      keyboardResults[action.shortcut || action.key] = { passed, evidence };
    }
    else if (action.type === 'reset') {
      const target = await findTarget(rootHandle, { label: action.label || 'Clear' });
      await callTool('click_element', { elementHandle: target.handle });
    }
    else if (action.type === 'fill') {
      const target = await findTarget(rootHandle, action);
      await callTool('set_element_value', { elementHandle: target.handle, value: String(action.value ?? '') });
    } else if (action.type === 'click') {
      const target = await findTarget(rootHandle, action);
      await callTool('click_element', { elementHandle: target.handle });
    } else throw new Error(`unsupported native capture action type: ${action.type}`);
    await new Promise((resolve) => setTimeout(resolve, Number(action.settleMs ?? 150)));
  }
  const eventRecording = actions.length ? structured(await callTool('stop_event_recording', { windowHandle })) : null;
  const tree = await getTree(rootHandle);
  const expectedTitles = { 'sales-voucher': 'Sales Counter', 'item-search': 'Item Search', 'invoice-list': 'Invoice List' };
  const expectedTitle = expectedTitles[screenId];
  const titleVisibleInHeader = expectedTitle && (tree.elements || []).some((element) => {
    const position = element.absolutePosition || {};
    return element.accessibleLabel === expectedTitle && (position.x || 0) < 160 && (position.y || 0) < 120;
  });
  if (expectedTitle && !titleVisibleInHeader) {
    throw new Error(`native window does not show ${expectedTitle} in the left header; refusing to file evidence as ${screenId}`);
  }
  const screenshot = await callTool('take_screenshot', { windowHandle, imageMimeType: 'image/png' });
  const image = screenshot.content?.find((item) => item.type === 'image');
  if (!image?.data) throw new Error(`No PNG image in screenshot response: ${JSON.stringify(screenshot).slice(0, 1000)}`);
  const screenshotPath = path.join(outDir, 'native.png');
  await fs.writeFile(screenshotPath, Buffer.from(image.data, 'base64'));
  const manifest = {
    schemaVersion: 1,
    screenId,
    state: stateName,
    actions,
    capturedAt: new Date().toISOString(),
    server: initialized.serverInfo || {},
    screenshot: 'native.png',
    screenshotSha256: crypto.createHash('sha256').update(await fs.readFile(screenshotPath)).digest('hex'),
    window: properties,
    elementCount: tree.elements?.length ?? tree.length ?? 0,
    toolNames: (tools.tools || []).map((tool) => tool.name),
  };
  if (eventRecording) await fs.writeFile(path.join(outDir, 'event-recording.json'), JSON.stringify(eventRecording, null, 2) + String.fromCharCode(10));
  await fs.writeFile(path.join(outDir, 'action-trace.json'), JSON.stringify(actionTrace, null, 2) + String.fromCharCode(10));
  await fs.writeFile(path.join(outDir, 'keyboard-results.json'), JSON.stringify({ shortcuts: keyboardResults }, null, 2) + String.fromCharCode(10));
  await fs.writeFile(path.join(outDir, 'window.json'), JSON.stringify(properties, null, 2) + '\n');
  await fs.writeFile(path.join(outDir, 'element-tree.json'), JSON.stringify(tree, null, 2) + '\n');
  await fs.writeFile(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  console.log(`captured native Slint evidence into ${outDir}; elements=${manifest.elementCount}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
