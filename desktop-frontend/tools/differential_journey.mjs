#!/usr/bin/env node
/* Model-based differential user-journey harness: Playwright web vs Slint MCP native. */
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
const require = createRequire(new URL('../../perf-suite/browser/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
function arg(n,f=''){const i=process.argv.indexOf('--'+n);return i>=0?process.argv[i+1]:f}
const baseUrl=arg('base-url','http://127.0.0.1:18086').replace(/\/$/,'');
const username=arg('username','Administrator'),password=arg('password',process.env.ERPNEXT_PASSWORD||'');
const out=path.resolve(arg('out','desktop-frontend/evidence/journeys/sales-voucher-differential.json'));
const exe=arg('exe','desktop-frontend/target/debug/khitmee-desktop.exe');
const port=Number(arg('port','49390'));
const sequence=JSON.parse(arg('sequence','[{"op":"f9"},{"op":"scan","value":"8850000000010"},{"op":"increase"},{"op":"decrease"},{"op":"quantity","value":"2.5"},{"op":"clear"},{"op":"search","value":"Mekong"}]'));
let nextId=1;
async function rpc(method,params={}){const r=await fetch(`http://127.0.0.1:${port}/mcp`,{method:'POST',headers:{'content-type':'application/json',accept:'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:nextId++,method,params})});const text=await r.text();const data=text.startsWith('data:')||text.startsWith('event:')?text.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).join('\n'):text;const parsed=JSON.parse(data);if(parsed.error)throw Error(JSON.stringify(parsed.error));return parsed.result}
function structured(result){if(result.structuredContent)return result.structuredContent;const text=result.content?.find(x=>x.type==='text')?.text;try{return JSON.parse(text)}catch{return result}}
async function call(name,args={}){const result=await rpc('tools/call',{name,arguments:args});if(result.isError)throw Error(`${name}: ${JSON.stringify(result)}`);return structured(result)}
const special={Enter:'\n',F2:String.fromCharCode(0xF705),F9:String.fromCharCode(0xF70C)};
function sleep(ms){return new Promise(r=>setTimeout(r,ms))}
async function nativeReady(){try{await rpc('initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'journey',version:'1'}});return true}catch{return false}}
async function nativeContext(){const windowsResult=structured(await call('list_windows'));const ws=windowsResult.windows||windowsResult.windowHandles||windowsResult;const wh=(Array.isArray(ws)?ws[0]:ws[0]).windowHandle||(Array.isArray(ws)?ws[0]:ws[0]).handle||(Array.isArray(ws)?ws[0]:ws[0]);const props=structured(await call('get_window_properties',{windowHandle:wh}));return{wh,root:props.rootElementHandle||props.root_element_handle||props.rootElement?.handle}}
async function tree(ctx){return structured(await call('get_element_tree',{elementHandle:ctx.root,maxElements:1200}))}
function matches(e,action){if(action.elementId)return(e.typeNamesAndIds||[]).some(x=>typeof x==='string'?x===action.elementId||x.endsWith('::'+action.elementId):x.id===action.elementId||(x.id&&x.id.endsWith('::'+action.elementId)));if(action.label)return e.accessibleLabel===action.label;return false}
async function find(ctx,action){const t=await tree(ctx);const e=(t.elements||[]).find(x=>matches(x,action));if(!e?.handle)throw Error('native target missing '+JSON.stringify(action));return e}
async function nativeAct(ctx,a){
 if(a.op==='wait')return sleep(a.ms||250);
 if(a.op==='key'||a.op==='f2'||a.op==='f9'){await call('dispatch_key_event',{windowHandle:ctx.wh,text:a.op==='f2'?special.F2:a.op==='f9'?special.F9:(special[a.key]||a.key),eventType:'PressAndRelease'});return sleep(250)}
 if(a.op==='search'||a.op==='scan'){const target=await find(ctx,{elementId:'item-search'});await call('set_element_value',{elementHandle:target.handle,value:String(a.value)});await sleep(500);if(a.op==='scan'){await call('dispatch_key_event',{windowHandle:ctx.wh,text:special.Enter,eventType:'PressAndRelease'});await sleep(400)}return}
 if(a.op==='increase'||a.op==='decrease'||a.op==='remove'||a.op==='clear'){const label={increase:'Increase quantity',decrease:'Decrease quantity',remove:'Remove line',clear:'Clear'}[a.op];const target=await find(ctx,{label});await call('click_element',{elementHandle:target.handle});return sleep(250)}
 if(a.op==='quantity'){const target=(await tree(ctx)).elements?.find(e=>e.accessibleLabel==='Quantity');if(!target)throw Error('quantity target missing');await call('set_element_value',{elementHandle:target.handle,value:String(a.value)});await sleep(250);return}
 if(a.op==='choose'){const rows=(await tree(ctx)).elements?.filter(e=>e.accessibleRole==='ListItem');const target=rows[a.index||0];if(!target)throw Error('choice missing');await call('click_element',{elementHandle:target.handle});return sleep(300)}
 throw Error('unsupported native op '+a.op)
}
function texts(t){return(t.elements||[]).filter(e=>e.accessibleRole==='Text').map(e=>({text:e.accessibleLabel||'',x:e.absolutePosition?.x||0,y:e.absolutePosition?.y||0}))}
async function nativeObs(ctx){const t=await tree(ctx),els=t.elements||[],tx=texts(t);const at=(x,y)=>tx.filter(e=>Math.abs(e.x-x)<8&&Math.abs(e.y-y)<8).map(e=>e.text).join(' ');const quantities=els.filter(e=>e.accessibleLabel==='Quantity').map(e=>e.accessibleValue||'');const submit=els.find(e=>e.accessibleLabel==='Submit & Print  F9');const status=tx.filter(e=>Math.abs(e.y-714)<4&&e.x>240&&e.x<300).map(e=>e.text).join(' ');return{status,totalQty:at(102,707),subtotal:at(224,707),submitDisabled:submit?submit.accessibleEnabled!==true:true,lineCount:quantities.length,quantities,searchExpanded:String((els.find(e=>e.accessibleLabel==='Barcode / item search F2')?.accessibleExpanded==='true')||els.filter(e=>e.accessibleRole==='ListItem').length>0),resultCount:els.filter(e=>e.accessibleRole==='ListItem').length,firstResult:(els.find(e=>e.accessibleRole==='ListItem')?.accessibleLabel||'').replace(/\s+/g,' ')}}
async function webObs(page){return page.evaluate(()=>({status:document.querySelector('#status')?.textContent.trim()||'',totalQty:document.querySelector('#total-qty')?.textContent.trim()||'',subtotal:document.querySelector('#subtotal')?.textContent.trim()||'',submitDisabled:Boolean(document.querySelector('#submit-print')?.disabled),lineCount:document.querySelectorAll('#lines-body tr').length,quantities:[...document.querySelectorAll('#lines-body input[aria-label="Quantity"]')].map(x=>x.value),searchExpanded:document.querySelector('#item-search')?.getAttribute('aria-expanded')||'false',resultCount:document.querySelectorAll('#search-results .search-option-button').length,firstResult:(document.querySelector('#search-results .search-option-button')?.innerText||'').replace(/\s+/g,' ')}))}
async function webAct(page,a){
 if(a.op==='wait')return page.waitForTimeout(a.ms||250);
 if(a.op==='key'||a.op==='f2'||a.op==='f9'){await page.keyboard.press(a.op==='f2'?'F2':a.op==='f9'?'F9':a.key);return page.waitForTimeout(250)}
 if(a.op==='search'||a.op==='scan'){await page.locator('#item-search').fill(String(a.value));await page.waitForTimeout(650);if(a.op==='scan'){await page.locator('#item-search').press('Enter');await page.waitForTimeout(400)}return}
 if(a.op==='increase'||a.op==='decrease'||a.op==='remove'||a.op==='clear'){const label={increase:'Increase quantity',decrease:'Decrease quantity',remove:'Remove line',clear:'Clear'}[a.op];if(a.op==='clear')await page.getByRole('button',{name:'Clear'}).first().click();else await page.getByLabel(label).first().click();return page.waitForTimeout(250)}
 if(a.op==='quantity'){await page.locator('input[aria-label="Quantity"]').first().fill(String(a.value));await page.waitForTimeout(250);return}
 if(a.op==='choose'){await page.locator('#search-results .search-option-button').nth(a.index||0).click();return page.waitForTimeout(300)}
 throw Error('unsupported web op '+a.op)
}
function compare(a,b){const diffs=[];for(const k of Object.keys(a)){let x=a[k],y=b[k];if(k==='firstResult'){x=String(x).replace(/\s+/g,' ');y=String(y).replace(/\s+/g,' ')}if(JSON.stringify(x)!==JSON.stringify(y))diffs.push({field:k,web:x,native:y})}return diffs}
async function main(){
 const env={...process.env,SLINT_MCP_PORT:String(port),SLINT_EMIT_DEBUG_INFO:'1',KHITMEE_CATALOG_ROWS:'10000',KHITMEE_INVOICE_ROWS:'10000',KHITMEE_ERP_URL:baseUrl,KHITMEE_ERP_USERNAME:username,KHITMEE_ERP_PASSWORD:password,KHITMEE_ERP_WAREHOUSE:'Dist Hub 1 - KMD',KHITMEE_ERP_CUSTOMER:'KM-CUST-0001',KHITMEE_ERP_PRICE_LIST:'Standard Selling'};
 const child=spawn(path.resolve(exe),{cwd:path.resolve('desktop-frontend'),env,stdio:'ignore'});
 try{
  const deadline=Date.now()+30000;
  while(Date.now()<deadline&&!(await nativeReady()))await sleep(100);
  if(!(await nativeReady()))throw Error('native MCP did not start');
  await sleep(1200);
  const launch={headless:true};const devinChrome='C:/devin/chrome/chrome-win64/chrome.exe';if(process.platform==='win32'&&existsSync(devinChrome))launch.executablePath=devinChrome;else if(process.platform==='win32')launch.channel='chrome';const browser=await chromium.launch(launch);
  const page=await (await browser.newContext({viewport:{width:1180,height:760},deviceScaleFactor:1})).newPage();
  if(password){await page.goto(baseUrl+'/login');await page.fill('#login_email',username);await page.fill('#login_password',password);await Promise.all([page.waitForLoadState('networkidle').catch(()=>{}),page.click('.btn-login,button[type="submit"]')])}
  await page.goto(baseUrl+'/khitmee_sales');await page.waitForLoadState('networkidle').catch(()=>{});await page.waitForTimeout(700);
  const ctx=await nativeContext();const steps=[];const failures=[];
  for(const [i,a] of sequence.entries()){
   await webAct(page,a);await nativeAct(ctx,a);await sleep(200);
   const web=await webObs(page),native=await nativeObs(ctx),diffs=compare(web,native);
   steps.push({index:i,action:a,web,native,differences:diffs});
   failures.push(...diffs.map(d=>({step:i,op:a.op,...d})));
  }
  await browser.close();
  const report={schemaVersion:1,passed:failures.length===0,failures,steps};
  await fs.mkdir(path.dirname(out),{recursive:true});await fs.writeFile(out,JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({passed:report.passed,failureCount:failures.length,out}));
  process.exitCode=report.passed?0:1;
 } finally{child.kill()}
}
main().catch(e=>{console.error(e);process.exitCode=2})
