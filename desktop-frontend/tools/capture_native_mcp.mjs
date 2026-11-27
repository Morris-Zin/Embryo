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
const outDir = path.resolve(arg('out', 'desktop-frontend/evidence/native'));
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
  const tree = structured(await callTool('get_element_tree', { elementHandle: rootHandle, maxElements: 1000 }));
  const screenshot = await callTool('take_screenshot', { windowHandle, imageMimeType: 'image/png' });
  const image = screenshot.content?.find((item) => item.type === 'image');
  if (!image?.data) throw new Error(`No PNG image in screenshot response: ${JSON.stringify(screenshot).slice(0, 1000)}`);
  const screenshotPath = path.join(outDir, 'native.png');
  await fs.writeFile(screenshotPath, Buffer.from(image.data, 'base64'));
  const manifest = {
    schemaVersion: 1,
    capturedAt: new Date().toISOString(),
    server: initialized.serverInfo || {},
    screenshot: 'native.png',
    screenshotSha256: crypto.createHash('sha256').update(await fs.readFile(screenshotPath)).digest('hex'),
    window: properties,
    elementCount: tree.elements?.length ?? tree.length ?? 0,
    toolNames: (tools.tools || []).map((tool) => tool.name),
  };
  await fs.writeFile(path.join(outDir, 'window.json'), JSON.stringify(properties, null, 2) + '\n');
  await fs.writeFile(path.join(outDir, 'element-tree.json'), JSON.stringify(tree, null, 2) + '\n');
  await fs.writeFile(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  console.log(`captured native Slint evidence into ${outDir}; elements=${manifest.elementCount}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
