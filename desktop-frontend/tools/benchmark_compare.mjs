#!/usr/bin/env node
/* Comparative sales-counter interaction benchmark: original web, PR #8 rust catalog mode, native Slint. */
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { existsSync } from 'node:fs';
const require=createRequire(new URL('../../perf-suite/browser/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
function arg(n,f=''){const i=process.argv.indexOf('--'+n);return i>=0?process.argv[i+1]:f}
const baseUrl=arg('base-url','http://127.0.0.1:18086').replace(/\/$/,'');
const username=arg('username','Administrator'),password=arg('password',process.env.ERPNEXT_PASSWORD||'');
const token=arg('desktop-token',process.env.KHITMEE_DESKTOP_TOKEN||'');
const iterations=Number(arg('iterations','5'));
const screen=arg('screen','sales-voucher');
const route=screen==='stock-transfer'?'/khitmee_transfer':'/khitmee_sales';
const out=path.resolve(arg('out','desktop-frontend/evidence/benchmarks/sales-voucher-comparison.json'));
const nativeExe=path.resolve(arg('native-exe','desktop-frontend/target/debug/khitmee-desktop.exe'));
const serviceExe=path.resolve(arg('service-exe','desktop-client/target/release/khitmee-catalog-service.exe'));
const port=Number(arg('port','49391'));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const barcodes=['8850000000010','8850000000027','8850000000034','8850000000041','8850000000058','8850000000065','8850000000072','8850000000089','8850000000096','8850000000102','8850000000119','8850000000126','8850000000133','8850000000140','8850000000157','8850000000164','8850000000171','8850000000188','8850000000195','8850000000201'];
let nextId=1;
async function rpc(method,params={}){const r=await fetch(`http://127.0.0.1:${port}/mcp`,{method:'POST',headers:{'content-type':'application/json',accept:'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:nextId++,method,params})});const text=await r.text();const data=text.startsWith('data:')||text.startsWith('event:')?text.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).join('\n'):text;const parsed=JSON.parse(data);if(parsed.error)throw Error(JSON.stringify(parsed.error));return parsed.result}
function structured(result){if(result.structuredContent)return result.structuredContent;const text=result.content?.find(x=>x.type==='text')?.text;try{return JSON.parse(text)}catch{return result}}
async function call(name,args={}){const result=await rpc('tools/call',{name,arguments:args});if(result.isError)throw Error(`${name}: ${JSON.stringify(result)}`);return structured(result)}
async function nativeReady(){try{await rpc('initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'bench',version:'1'}});return true}catch{return false}}
async function nativeContext(){const windowsResult=structured(await call('list_windows'));const ws=windowsResult.windows||windowsResult.windowHandles||windowsResult;const first=(Array.isArray(ws)?ws[0]:ws[0]);const wh=first.windowHandle||first.handle||first;const props=structured(await call('get_window_properties',{windowHandle:wh}));return{wh,root:props.rootElementHandle||props.root_element_handle||props.rootElement?.handle}}
async function tree(ctx){return structured(await call('get_element_tree',{elementHandle:ctx.root,maxElements:1200}))}
async function nativeObs(ctx){const els=(await tree(ctx)).elements||[];return{lineCount:els.filter(e=>e.accessibleLabel==='Quantity').length,resultCount:els.filter(e=>e.accessibleRole==='ListItem').length,quantities:els.filter(e=>e.accessibleLabel==='Quantity').map(e=>e.accessibleValue||'')}}
async function waitNative(ctx,predicate,timeout=3000){const start=performance.now();while(performance.now()-start<timeout){const obs=await nativeObs(ctx);if(predicate(obs))return{obs,ms:performance.now()-start};await sleep(25)}throw Error('native benchmark predicate timed out')}
async function waitWeb(page,predicate,timeout=3000){const start=performance.now();while(performance.now()-start<timeout){const obs=await page.evaluate(()=>({lineCount:document.querySelectorAll('#lines-body tr').length,resultCount:document.querySelectorAll('#search-results .search-option-button').length,quantities:[...document.querySelectorAll('#lines-body input[aria-label="Quantity"]')].map(x=>x.value)}));if(predicate(obs))return{obs,ms:performance.now()-start};await sleep(25)}throw Error('web benchmark predicate timed out')}
async function benchWeb(page,mode){
 await page.goto(`${baseUrl}${route}?khitmee_catalog=${mode}`);await page.waitForLoadState('networkidle').catch(()=>{});await page.waitForTimeout(700);
 // Prime the identical local-catalog synchronization path before measuring warm interactions.
 await page.locator('#item-search').fill('Mekong');
 await waitWeb(page,o=>o.resultCount>0,10000).catch(()=>{throw Error(mode+' catalog warmup produced no results: '+JSON.stringify({status:'unavailable'}))});
 await page.keyboard.press('Escape').catch(()=>{});await page.locator('#item-search').fill('');await page.waitForTimeout(250);
 const samples=[];
 for(let i=0;i<iterations;i++){
  await page.locator('#item-search').fill('Mekong');
  const search=await waitWeb(page,o=>o.resultCount>0);
  await page.keyboard.press('Escape').catch(()=>{});
  const barcode=barcodes[i%barcodes.length];await page.locator('#item-search').fill(barcode);
  const before=await page.evaluate(()=>document.querySelectorAll('#lines-body tr').length);
  const scanStart=performance.now();await page.locator('#item-search').press('Enter');
  const scan=await waitWeb(page,o=>o.lineCount>before).catch(async()=>({ms:performance.now()-scanStart,obs:await page.evaluate(()=>({lineCount:document.querySelectorAll('#lines-body tr').length}))}));
  await page.getByRole('button',{name:'Clear'}).first().click();await page.waitForTimeout(180);
  samples.push({barcode,searchMs:search.ms,scanMs:scan.ms,searchResults:search.obs.resultCount});
 }
 return aggregate(samples);
}
async function benchNative(ctx){
 const searchTarget=async()=>{const t=await tree(ctx);const e=(t.elements||[]).find(x=>(x.typeNamesAndIds||[]).some(v=>typeof v==='string'?v==='item-search'||v.endsWith('::item-search'):v.id==='item-search'||(v.id&&v.id.endsWith('::item-search'))));if(!e)throw Error('native search target missing');return e};
 const samples=[];
 for(let i=0;i<iterations;i++){
  const target=await searchTarget();
  await call('set_element_value',{elementHandle:target.handle,value:'Mekong'});
  const search=await waitNative(ctx,o=>o.resultCount>0);
  const barcode=barcodes[i%barcodes.length];await call('set_element_value',{elementHandle:target.handle,value:barcode});
  const before=(await nativeObs(ctx)).lineCount;
  const scanStart=performance.now();await call('dispatch_key_event',{windowHandle:ctx.wh,text:'\n',eventType:'PressAndRelease'});
  const scan=await waitNative(ctx,o=>o.lineCount>before).catch(async()=>({ms:performance.now()-scanStart,obs:await nativeObs(ctx)}));
  const t=await tree(ctx);const clear=(t.elements||[]).find(e=>e.accessibleLabel==='Clear');if(clear)await call('click_element',{elementHandle:clear.handle});await sleep(180);
  samples.push({barcode,searchMs:search.ms,scanMs:scan.ms,searchResults:search.obs.resultCount});
 }
 return aggregate(samples);
}
function aggregate(samples){const values=key=>samples.map(x=>x[key]).sort((a,b)=>a-b);const stat=key=>{const xs=values(key);return{mean:xs.reduce((a,b)=>a+b,0)/xs.length,p50:xs[Math.floor((xs.length-1)/2)],p95:xs[Math.min(xs.length-1,Math.ceil(xs.length*.95)-1)],min:xs[0],max:xs[xs.length-1]}};return{samples,searchMs:stat('searchMs'),scanMs:stat('scanMs')}}
async function main(){
 if(!token)throw Error('KHITMEE_DESKTOP_TOKEN is required for the PR8 rust service benchmark');
 const tmp=await fs.mkdtemp(path.join(os.tmpdir(),'khitmee-bench-'));
 const service=spawn(serviceExe,['serve','--db',path.join(tmp,'catalog.db'),'--key',path.join(tmp,'catalog.key'),'--port','18422','--allowed-origin',baseUrl,'--token',token],{stdio:'ignore'});
 const native=spawn(nativeExe,{cwd:path.resolve('desktop-frontend'),env:{...process.env,SLINT_MCP_PORT:String(port),SLINT_EMIT_DEBUG_INFO:'1',KHITMEE_CATALOG_ROWS:'10000',KHITMEE_INVOICE_ROWS:'10000',KHITMEE_ERP_URL:baseUrl,KHITMEE_ERP_USERNAME:username,KHITMEE_ERP_PASSWORD:password,KHITMEE_ERP_WAREHOUSE:'Dist Hub 1 - KMD',KHITMEE_ERP_CUSTOMER:'KM-CUST-0001',KHITMEE_ERP_PRICE_LIST:'Standard Selling',KHITMEE_INITIAL_SCREEN:screen==='stock-transfer'?'stock-transfer':'sales'},stdio:'ignore'});
 try{
  const deadline=Date.now()+30000;while(Date.now()<deadline&&!(await nativeReady()))await sleep(100);
  const launch={headless:true};const devinChrome='C:/devin/chrome/chrome-win64/chrome.exe';if(process.platform==='win32'&&existsSync(devinChrome))launch.executablePath=devinChrome;else if(process.platform==='win32')launch.channel='chrome';
  const browser=await chromium.launch(launch);const page=await (await browser.newContext({viewport:{width:1180,height:760},deviceScaleFactor:1})).newPage();
  await page.goto(baseUrl+'/login');await page.fill('#login_email',username);await page.fill('#login_password',password);await Promise.all([page.waitForLoadState('networkidle').catch(()=>{}),page.click('.btn-login,button[type="submit"]')]);
  const originalWeb=await benchWeb(page,'off');
  const pr8RustLoopback=await benchWeb(page,'rust');
  const ctx=await nativeContext();const nativeSlint=await benchNative(ctx);
  await browser.close();
  const report={schemaVersion:1,generatedAt:new Date().toISOString(),iterations,workload:{screen,searchTerm:'Mekong',barcodes,catalogRows:10000,note:'wall-clock UI latency from input action to observable result/line; includes web debounce/rendering and native reactive rendering'},results:{originalWeb,pr8RustLoopback,nativeSlint},pendingRequiredMeasurements:['cross-process memory comparison','frame latency during scrolling','network byte totals','barcode hardware latency','printer latency','low-end Windows hardware']};
  await fs.mkdir(path.dirname(out),{recursive:true});await fs.writeFile(out,JSON.stringify(report,null,2)+String.fromCharCode(10));
  console.log(JSON.stringify({out,web:originalWeb.scanMs.p50,pr8:pr8RustLoopback.scanMs.p50,native:nativeSlint.scanMs.p50}));
 }finally{service.kill();native.kill()}
}
main().catch(e=>{console.error(e);process.exitCode=2})
