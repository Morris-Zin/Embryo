#!/usr/bin/env node
/* Cold-open and item-grid interaction benchmark: ERPNext web Sales Invoice detail vs native Slint. */
import { createRequire } from 'node:module';
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { existsSync } from 'node:fs';
const require=createRequire(new URL('../../perf-suite/browser/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
function arg(n,f=''){const i=process.argv.indexOf('--'+n);return i>=0?process.argv[i+1]:f}
const baseUrl=arg('base-url','http://127.0.0.1:18086').replace(/\/$/,'');
const username=arg('username','Administrator'),password=arg('password',process.env.ERPNEXT_PASSWORD||'');
const invoice=arg('invoice','ACC-SINV-2026-04937');
const iterations=Number(arg('iterations','5'));
const out=path.resolve(arg('out','desktop-frontend/evidence/benchmarks/invoice-detail-comparison.json'));
const exe=path.resolve(arg('exe','desktop-frontend/target/debug/khitmee-desktop.exe'));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let nextId=1,port=49410;
async function rpc(method,params={}){const r=await fetch(`http://127.0.0.1:${port}/mcp`,{method:'POST',headers:{'content-type':'application/json',accept:'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:nextId++,method,params})});const text=await r.text();const data=text.startsWith('data:')||text.startsWith('event:')?text.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).join('\n'):text;const parsed=JSON.parse(data);if(parsed.error)throw Error(JSON.stringify(parsed.error));return parsed.result}
function structured(result){if(result.structuredContent)return result.structuredContent;const text=result.content?.find(x=>x.type==='text')?.text;try{return JSON.parse(text)}catch{return result}}
async function call(name,args={}){const result=await rpc('tools/call',{name,arguments:args});if(result.isError)throw Error(`${name}: ${JSON.stringify(result)}`);return structured(result)}
async function nativeReady(){try{await rpc('initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'invoice-bench',version:'1'}});return true}catch{return false}}
async function nativeContext(){const ws=structured(await call('list_windows'));const list=ws.windows||ws.windowHandles||ws;const first=list[0];const wh=first.windowHandle||first.handle||first;const props=structured(await call('get_window_properties',{windowHandle:wh}));return{wh,root:props.rootElementHandle||props.root_element_handle||props.rootElement?.handle}}
async function tree(ctx){return structured(await call('get_element_tree',{elementHandle:ctx.root,maxElements:1000}))}
async function nativeHas(ctx,fn){try{return fn((await tree(ctx)).elements||[])}catch{return false}}
function stat(values){const xs=[...values].sort((a,b)=>a-b);return{mean:xs.reduce((a,b)=>a+b,0)/xs.length,p50:xs[Math.floor((xs.length-1)/2)],min:xs[0],max:xs[xs.length-1]}}
function processMemoryMb(pid){try{const raw=execFileSync('powershell.exe',['-NoProfile','-Command',`(Get-Process -Id ${pid}).WorkingSet64`],{encoding:'utf8',timeout:3000});return Number(raw.trim())/1048576}catch{return null}}
async function benchWebOnce(){
 const browser=await chromium.launch({headless:true,executablePath:existsSync('C:/devin/chrome/chrome-win64/chrome.exe')?'C:/devin/chrome/chrome-win64/chrome.exe':undefined});
 const context=await browser.newContext({viewport:{width:1180,height:760},deviceScaleFactor:1});const page=await context.newPage();
 await context.request.post(baseUrl+'/api/method/login',{data:{usr:username,pwd:password}});
 const coldStart=performance.now();
 await page.goto(baseUrl+'/app/sales-invoice/'+encodeURIComponent(invoice));
 await page.locator('.frappe-control[data-fieldname="customer"]').first().waitFor({state:'visible',timeout:10000});
 await page.locator('.frappe-control[data-fieldname="items"] .grid-row[data-idx="1"]').first().waitFor({state:'visible',timeout:10000});
 const coldOpenMs=performance.now()-coldStart;
 const rowStart=performance.now();
 await page.locator('.frappe-control[data-fieldname="items"] .grid-row[data-idx="1"]').first().click();
 await page.locator('.frappe-control[data-fieldname="items"] input[data-fieldname="qty"]:visible').first().waitFor({state:'visible',timeout:5000});
 const rowOpenMs=performance.now()-rowStart;
 const qtyInput=page.locator('.frappe-control[data-fieldname="items"] input[data-fieldname="qty"]:visible').first();
 const editStart=performance.now();await qtyInput.fill('2');await qtyInput.press('Enter').catch(()=>{});
 const deadline=performance.now()+5000;let editMs=5000;
 while(performance.now()<deadline){const total=await page.locator('.frappe-control[data-fieldname="total_qty"] .control-value').first().innerText().catch(()=>'');if(String(total).trim()==='2'){editMs=performance.now()-editStart;break}await sleep(25)}
 await browser.close();
 return{coldOpenMs,rowOpenMs,qtyEditMs:editMs};
}
async function benchNativeOnce(iteration){
 port=49410+iteration;nextId=1;
 const start=performance.now();
 const child=spawn(exe,{cwd:path.resolve('desktop-frontend'),env:{...process.env,SLINT_MCP_PORT:String(port),SLINT_EMIT_DEBUG_INFO:'1',KHITMEE_CATALOG_ROWS:'10000',KHITMEE_INVOICE_ROWS:'10000',KHITMEE_ERP_URL:baseUrl,KHITMEE_ERP_USERNAME:username,KHITMEE_ERP_PASSWORD:password,KHITMEE_ERP_WAREHOUSE:'Dist Hub 1 - KMD',KHITMEE_ERP_CUSTOMER:'KM-CUST-0001',KHITMEE_ERP_PRICE_LIST:'Standard Selling',KHITMEE_INITIAL_SCREEN:'invoice-detail',KHITMEE_INVOICE_NAME:invoice},stdio:'ignore'});
 try{
  const deadline=Date.now()+15000;let ctx=null,coldOpenMs=15000;
  while(Date.now()<deadline){
   if(await nativeReady()){
    ctx=await nativeContext();
    if(await nativeHas(ctx,els=>els.some(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel==='Customer')&&els.some(e=>e.accessibleRole==='ListItem'&&String(e.accessibleLabel||'').startsWith('1 ')))){coldOpenMs=performance.now()-start;break}
   }
   await sleep(50)
  }
  if(!ctx)throw Error('native benchmark MCP did not start');
  const memoryMb=processMemoryMb(child.pid);
  let rowOpenMs=5000,rowHandle=null;const rowDeadline=performance.now()+5000;
  while(performance.now()<rowDeadline&&!rowHandle){const els=(await tree(ctx)).elements||[];const row=els.find(e=>e.accessibleRole==='ListItem'&&String(e.accessibleLabel||'').startsWith('1 '));if(row)rowHandle=row.handle;await sleep(30)}
  if(!rowHandle)throw Error('native benchmark row missing');
  const rowStart=performance.now();await call('click_element',{elementHandle:rowHandle});
  const editDeadline=performance.now()+5000;let qtyHandle=null;
  while(performance.now()<editDeadline&&!qtyHandle){const els=(await tree(ctx)).elements||[];const input=els.find(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel==='Item quantity');if(input)qtyHandle=input.handle;await sleep(25)}
  if(!qtyHandle)throw Error('native benchmark qty input missing');
  rowOpenMs=performance.now()-rowStart;
  const editStart=performance.now();await call('set_element_value',{elementHandle:qtyHandle,value:'2'});
  const qtyDeadline=performance.now()+5000;let editMs=5000;
  while(performance.now()<qtyDeadline){const ok=await nativeHas(ctx,els=>els.some(e=>e.accessibleRole==='Text'&&String(e.accessibleLabel||'')==='Total Quantity: 2'));if(ok){editMs=performance.now()-editStart;break}await sleep(25)}
  return{coldOpenMs,rowOpenMs,qtyEditMs:editMs,memoryMb};
 }finally{child.kill()}
}
async function main(){
 if(!password)throw Error('ERPNEXT_PASSWORD is required');
 const web=[],native=[];
 for(let i=0;i<iterations;i++)web.push(await benchWebOnce());
 for(let i=0;i<iterations;i++)native.push(await benchNativeOnce(i));
 const summarize=rows=>({samples:rows,coldOpenMs:stat(rows.map(x=>x.coldOpenMs)),rowOpenMs:stat(rows.map(x=>x.rowOpenMs)),qtyEditMs:stat(rows.map(x=>x.qtyEditMs)),memoryMb:rows.some(x=>x.memoryMb!=null)?stat(rows.filter(x=>x.memoryMb!=null).map(x=>x.memoryMb)):null});
 const report={schemaVersion:1,generatedAt:new Date().toISOString(),invoice,iterations,workload:{catalogRows:10000,invoiceRows:10000,coldOpen:'direct Sales Invoice detail until customer and first item row visible',rowOpen:'click first item row until quantity input visible',qtyEdit:'set quantity to 2 until Total Quantity 2 observable',note:'wall-clock UI latency; native observation uses Slint MCP accessibility polling and browser observation uses DOM polling'},results:{web:summarize(web),native:summarize(native)},pendingRequiredMeasurements:['cross-process browser memory comparison','frame latency during scrolling','network byte totals','printer and barcode hardware latency','low-end Windows hardware']};
 await fs.mkdir(path.dirname(out),{recursive:true});await fs.writeFile(out,JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify({out,web:report.results.web.coldOpenMs.p50,native:report.results.native.coldOpenMs.p50}));
}
main().catch(e=>{console.error(e);process.exitCode=2})
