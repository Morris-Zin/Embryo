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
const scenario=arg('scenario','exact');
const screen=arg('screen','sales-voucher');
const webInvoice=arg('web-invoice',arg('invoice','ACC-SINV-2026-04935')),nativeInvoice=arg('native-invoice',arg('invoice','ACC-SINV-2026-04935'));
const sequence=JSON.parse(arg('sequence','[{"op":"f9"},{"op":"scan","value":"8850000000010"},{"op":"scroll","deltaY":500},{"op":"increase"},{"op":"decrease"},{"op":"quantity","value":"2.5"},{"op":"clear"},{"op":"search","value":"Mekong"}]'));
let nextId=1;
async function rpc(method,params={}){const r=await fetch(`http://127.0.0.1:${port}/mcp`,{method:'POST',headers:{'content-type':'application/json',accept:'application/json, text/event-stream'},body:JSON.stringify({jsonrpc:'2.0',id:nextId++,method,params})});const text=await r.text();const data=text.startsWith('data:')||text.startsWith('event:')?text.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).join('\n'):text;const parsed=JSON.parse(data);if(parsed.error)throw Error(JSON.stringify(parsed.error));return parsed.result}
function structured(result){if(result.structuredContent)return result.structuredContent;const text=result.content?.find(x=>x.type==='text')?.text;try{return JSON.parse(text)}catch{return result}}
async function call(name,args={}){const result=await rpc('tools/call',{name,arguments:args});if(result.isError)throw Error(`${name}: ${JSON.stringify(result)}`);return structured(result)}
const special={Enter:'\n',Escape:'\x1b',ArrowUp:String.fromCharCode(0xF700),ArrowDown:String.fromCharCode(0xF701),F2:String.fromCharCode(0xF705),F9:String.fromCharCode(0xF70C)};
function sleep(ms){return new Promise(r=>setTimeout(r,ms))}
async function nativeReady(){try{await rpc('initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'journey',version:'1'}});return true}catch{return false}}
async function nativeContext(){const windowsResult=structured(await call('list_windows'));const ws=windowsResult.windows||windowsResult.windowHandles||windowsResult;const wh=(Array.isArray(ws)?ws[0]:ws[0]).windowHandle||(Array.isArray(ws)?ws[0]:ws[0]).handle||(Array.isArray(ws)?ws[0]:ws[0]);const props=structured(await call('get_window_properties',{windowHandle:wh}));return{wh,root:props.rootElementHandle||props.root_element_handle||props.rootElement?.handle}}
async function tree(ctx){return structured(await call('get_element_tree',{elementHandle:ctx.root,maxElements:1000}))}
async function queryDescendants(ctx,queryStack){const response=structured(await call('query_element_descendants',{elementHandle:ctx.root,findAll:true,queryStack}));const handles=response.elementHandles||response.element_handles||[];return Promise.all(handles.map(async elementHandle=>({...(structured(await call('get_element_properties',{elementHandle}))),handle:elementHandle})))}
async function nativeChooseCombo(ctx,label,wanted,fallbackValues=[]){for(let attempt=0;attempt<4;attempt++){const t=await tree(ctx);const combos=(t.elements||[]).filter(e=>e.accessibleRole==='Combobox').map(e=>({label:e.accessibleLabel,value:e.accessibleValue,handle:e.handle,id:e.typeNamesAndIds}));if(process.env.DEBUG_COMBO)console.log('COMBOS',label,JSON.stringify(combos));const combo=(t.elements||[]).find(e=>e.accessibleRole==='Combobox'&&e.accessibleLabel===label)||(t.elements||[]).find(e=>e.accessibleRole==='Combobox'&&(fallbackValues.includes(e.accessibleValue)||e.accessibleValue===wanted));if(process.env.DEBUG_COMBO)console.log('CHOOSE',label,JSON.stringify(combo));if(combo)await call('click_element',{elementHandle:combo.handle});for(let poll=0;poll<10;poll++){await sleep(80);const openTree=await tree(ctx);if(process.env.DEBUG_COMBO&&poll===0)console.log('POPUPITEMS',JSON.stringify((openTree.elements||[]).filter(e=>e.accessibleRole==='ListItem').map(e=>({label:e.accessibleLabel,handle:e.handle})).slice(0,20)));const item=(openTree.elements||[]).find(e=>e.accessibleRole==='ListItem'&&e.accessibleLabel===wanted);if(item){await call('click_element',{elementHandle:item.handle});await sleep(350);return}}}throw Error('native combo option did not open: '+label+' / '+wanted)}
function matches(e,action){if(action.elementId)return(e.typeNamesAndIds||[]).some(x=>typeof x==='string'?x===action.elementId||x.endsWith('::'+action.elementId):x.id===action.elementId||(x.id&&x.id.endsWith('::'+action.elementId)));if(action.label)return e.accessibleLabel===action.label;return false}
async function find(ctx,action){const t=await tree(ctx);const e=(t.elements||[]).find(x=>matches(x,action));if(!e?.handle)throw Error('native target missing '+JSON.stringify(action));return e}
function submittedVoucher(steps,side){for(let i=steps.length-1;i>=0;i--){const match=String(steps[i]?.[side]?.status||'').match(/Submitted\s+([A-Z]+(?:-[A-Z]+)*-\d{4}-\d+)/);if(match)return match[1]}throw Error('no prior submitted voucher for '+side)}
function dynamicValue(a,steps,side){return a.value==='own-last-voucher'?submittedVoucher(steps,side):String(a.value||'')}
async function nativeAct(ctx,a,steps=[]){
 if(a.op==='wait')return sleep(a.ms||250);
 if(a.op==='key'||a.op==='f2'||a.op==='f9'){await call('dispatch_key_event',{windowHandle:ctx.wh,text:a.op==='f2'?special.F2:a.op==='f9'?special.F9:(special[a.key]||a.key),eventType:'PressAndRelease'});return sleep(250)}
 if(a.op==='search'||a.op==='scan'){const target=await find(ctx,{elementId:'item-search'});await call('click_element',{elementHandle:target.handle});await call('set_element_value',{elementHandle:target.handle,value:String(a.value)});await sleep(500);if(a.op==='scan'){await call('dispatch_key_event',{windowHandle:ctx.wh,text:special.Enter,eventType:'PressAndRelease'});await sleep(400)}return}
 if(a.op==='scroll'){await call('dispatch_pointer_scroll',{windowHandle:ctx.wh,position:{x:590,y:344},deltaY:-Math.abs(Number(a.deltaY||500)),deltaX:0});return sleep(350)}
 if(a.op==='increase'||a.op==='decrease'||a.op==='remove'||a.op==='clear'){const label={increase:'Increase quantity',decrease:'Decrease quantity',remove:'Remove line',clear:'Clear'}[a.op];const target=await find(ctx,{label});await call('click_element',{elementHandle:target.handle});return sleep(250)}
 if(a.op==='warehouse'){
  const wanted=String(a.value||'Shop 1');
  for(let attempt=0;attempt<4;attempt++){
   const t=await tree(ctx);
   const combo=(t.elements||[]).find(e=>e.accessibleRole==='Combobox'&&(e.accessibleValue===wanted||e.accessibleValue==='Dist Hub 1'||e.accessibleValue==='Dist Hub 2'||e.accessibleValue==='Shop 1'||e.accessibleValue==='Shop 2'));
   if(combo)await call('click_element',{elementHandle:combo.handle});
   for(let poll=0;poll<10;poll++){
     await sleep(80);
     const openTree=await tree(ctx);
     const item=(openTree.elements||[]).find(e=>e.accessibleRole==='ListItem'&&e.accessibleLabel===wanted);
     if(item){await call('click_element',{elementHandle:item.handle});await sleep(650);return}
    }
  }
  throw Error('native warehouse option did not open: '+wanted)
 }
 if(a.op==='customer'){
  const wanted=String(a.value||'Khit Customer 0001');
  for(let attempt=0;attempt<4;attempt++){
   const t=await tree(ctx);
   const combo=(t.elements||[]).find(e=>e.accessibleRole==='Combobox'&&(e.accessibleValue==='Choose customer'||e.accessibleValue===wanted||(!e.accessibleLabel&&e.accessibleValue)));
   if(combo)await call('click_element',{elementHandle:combo.handle});
   for(let poll=0;poll<10;poll++){
     await sleep(80);
     const openTree=await tree(ctx);
     const item=(openTree.elements||[]).find(e=>e.accessibleRole==='ListItem'&&e.accessibleLabel===wanted);
     if(item){await call('click_element',{elementHandle:item.handle});await sleep(350);return}
    }
  }
  throw Error('native customer option did not open: '+wanted)
 }
 if(a.op==='source-warehouse'){await nativeChooseCombo(ctx,'Source warehouse',String(a.value||'Dist Hub 1'),['Dist Hub 1']);return}
 if(a.op==='target-warehouse'){await nativeChooseCombo(ctx,'Target warehouse',String(a.value||'Shop 1'),['Choose target warehouse']);return}
 if(a.op==='transfer-mode'){await nativeChooseCombo(ctx,'Transfer mode',String(a.value||'Direct transfer'),['Direct transfer','Dispatch to transit','Receive from transit']);return}
 if(a.op==='outgoing-entry'){const target=(await tree(ctx)).elements?.find(e=>e.accessibleLabel==='Outgoing stock entry'&&e.accessibleRole==='TextInput');if(!target)throw Error('native outgoing entry target missing');await call('click_element',{elementHandle:target.handle});await call('set_element_value',{elementHandle:target.handle,value:dynamicValue(a,steps,'native')});await sleep(250);return}
 if(a.op==='open-first-invoice'){const rows=await queryDescendants(ctx,[{matchElementAccessibleRole:'ListItem'}]);const target=(Array.isArray(rows)?rows:[]).find(e=>e.accessibleRole==='ListItem');if(!target)throw Error('native invoice row missing');await call('click_element',{elementHandle:target.handle});await sleep(1400);return}
 if(a.op==='load-more'){const target=await find(ctx,{label:'Load More'});await call('click_element',{elementHandle:target.handle});await sleep(800);return}
 if(a.op==='invoice-sort-toggle'){const t=await tree(ctx);const target=(t.elements||[]).find(e=>e.accessibleRole==='Button'&&(e.accessibleLabel==='descending'||e.accessibleLabel==='ascending'));if(!target)throw Error('native invoice sort target missing');await call('click_element',{elementHandle:target.handle});await sleep(800);return}
 if(a.op==='invoice-filter'){const target=await find(ctx,{elementId:'invoice-filter'});await call('click_element',{elementHandle:target.handle});await call('set_element_value',{elementHandle:target.handle,value:String(a.value||'')});await sleep(800);return}
 if(a.op==='detail-field'){const target=(await tree(ctx)).elements?.find(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel===a.label);if(!target)throw Error('native detail field missing '+a.label);await call('click_element',{elementHandle:target.handle});await call('set_element_value',{elementHandle:target.handle,value:String(a.value)});await sleep(400);return}
 if(a.op==='detail-item-open'){const rows=await queryDescendants(ctx,[{matchElementAccessibleRole:'ListItem'}]);const target=(Array.isArray(rows)?rows:[]).find(e=>String(e.accessibleLabel||'').startsWith(String((a.index??0)+1)+' '));if(!target)throw Error('native detail item row missing');await call('click_element',{elementHandle:target.handle});await sleep(500);return}
 if(a.op==='detail-item-code'){const target=(await tree(ctx)).elements?.find(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel==='Item code');if(!target)throw Error('native item code input missing');await call('click_element',{elementHandle:target.handle});await call('set_element_value',{elementHandle:target.handle,value:String(a.value)});await sleep(700);return}
 if(a.op==='detail-item-qty-key'||a.op==='detail-item-rate-key'){const label=a.op==='detail-item-qty-key'?'Item quantity':'Item rate';const target=(await tree(ctx)).elements?.find(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel===label);if(!target)throw Error('native item key input missing '+label);await call('click_element',{elementHandle:target.handle});await call('dispatch_key_event',{windowHandle:ctx.wh,text:String(a.value),eventType:'PressAndRelease'});await call('dispatch_key_event',{windowHandle:ctx.wh,text:special.Enter,eventType:'PressAndRelease'});await sleep(600);return}
 if(a.op==='detail-item-qty'||a.op==='detail-item-rate'){const label=a.op==='detail-item-qty'?'Item quantity':'Item rate';const target=(await tree(ctx)).elements?.find(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel===label);if(!target)throw Error('native item input missing '+label);await call('click_element',{elementHandle:target.handle});await call('set_element_value',{elementHandle:target.handle,value:String(a.value)});await sleep(450);return}
 if(a.op==='detail-item-add'){const target=await find(ctx,{label:'Add row'});await call('click_element',{elementHandle:target.handle});await sleep(600);return}
 if(a.op==='detail-item-select'){const target=await find(ctx,{label:'Select row '+String((a.index??0)+1)});await call('click_element',{elementHandle:target.handle});await sleep(350);return}
 if(a.op==='detail-item-delete'){const target=await find(ctx,{label:'Delete row'});await call('click_element',{elementHandle:target.handle});await sleep(600);return}
 if(a.op==='detail-save'||a.op==='detail-submit'){const target=await find(ctx,{label:a.op==='detail-save'?'Save':'Submit'});await call('click_element',{elementHandle:target.handle});await sleep(a.op==='detail-save'?1800:2400);return}
 if(a.op==='quantity'){const target=(await tree(ctx)).elements?.find(e=>e.accessibleLabel==='Quantity');if(!target)throw Error('quantity target missing');await call('set_element_value',{elementHandle:target.handle,value:String(a.value)});await sleep(250);return}
 if(a.op==='choose'){const rows=(await tree(ctx)).elements?.filter(e=>e.accessibleRole==='ListItem');const target=rows[a.index||0];if(!target)throw Error('choice missing');await call('click_element',{elementHandle:target.handle});return sleep(300)}
 throw Error('unsupported native op '+a.op)
}
function texts(t){return(t.elements||[]).filter(e=>e.accessibleRole==='Text').map(e=>({text:e.accessibleLabel||'',x:e.absolutePosition?.x||0,y:e.absolutePosition?.y||0}))}
async function invoiceNativeObs(ctx){
 const parseRows=rowEls=>(Array.isArray(rowEls)?rowEls:[]).filter(e=>e.accessibleRole==='ListItem').map(e=>{const chunk=(e.accessibleLabel||'').split('|');return{customer:chunk[0]||'',status:chunk[1]||'',postingDate:chunk[2]||'',dueDate:chunk[3]||'',total:chunk[4]||'',id:chunk[5]||''}});
 const seen=new Set(),rows=[];
 let stagnant=0;
 for(let pass=0;pass<12&&stagnant<2;pass++){
  const visible=parseRows(await queryDescendants(ctx,[{matchElementAccessibleRole:'ListItem'}]));
  let added=0;
  for(const row of visible){if(row.id&&!seen.has(row.id)){seen.add(row.id);rows.push(row);added++}}
  if(pass>0&&added===0)stagnant++;else stagnant=0;
  if(pass<11){await call('dispatch_pointer_scroll',{windowHandle:ctx.wh,position:{x:590,y:344},deltaY:-700,deltaX:0});await sleep(180)}
 }
 for(let pass=0;pass<12;pass++){await call('dispatch_pointer_scroll',{windowHandle:ctx.wh,position:{x:590,y:344},deltaY:900,deltaX:0});await sleep(60)}
 const t=await tree(ctx),els=t.elements||[];const countEl=els.find(e=>/^\d+ of [\d,+]+$/.test(e.accessibleLabel||''));
 const permissionState=els.some(e=>String(e.accessibleLabel||'').startsWith('Not Permitted'))?'not-permitted':'permitted';
 return{rows,rowCount:rows.length,countText:countEl?.accessibleLabel||'',filterValue:(els.find(e=>e.accessibleLabel==='ID'&&e.accessibleRole==='TextInput')?.accessibleValue)||'',permissionState}
}
async function nativeObs(ctx){if(screen==='invoice-list')return invoiceNativeObs(ctx);if(screen==='invoice-detail')return invoiceDetailNativeObs(ctx);const t=await tree(ctx),els=t.elements||[],tx=texts(t);const at=(x,y)=>tx.filter(e=>Math.abs(e.x-x)<8&&Math.abs(e.y-y)<8).map(e=>e.text).join(' ');const quantities=els.filter(e=>e.accessibleLabel==='Quantity').map(e=>e.accessibleValue||'');const submit=els.find(e=>e.accessibleLabel==='Submit & Print  F9');const statusEl=els.find(e=>(e.typeNamesAndIds||[]).some(x=>typeof x==='string'?x.endsWith('::status-text'):x.id&&x.id.endsWith('::status-text')));const status=statusEl?.accessibleLabel||tx.filter(e=>Math.abs(e.y-714)<4&&e.x>240&&e.x<300).map(e=>e.text).join(' ');const observed={status,totalQty:at(102,707),subtotal:at(224,707),submitDisabled:submit?submit.accessibleEnabled!==true:true,lineCount:quantities.length,quantities,targetWarehouse:(els.filter(e=>e.accessibleRole==='Combobox').find(e=>e.accessibleLabel==='Target warehouse')?.accessibleValue)||'',transferMode:(els.filter(e=>e.accessibleRole==='Combobox').find(e=>e.accessibleLabel==='Transfer mode')?.accessibleValue)||'',outgoingDisabled:(els.find(e=>e.accessibleLabel==='Outgoing stock entry'&&e.accessibleRole==='TextInput')?.accessibleEnabled)!==true,outgoingValue:(els.find(e=>e.accessibleLabel==='Outgoing stock entry'&&e.accessibleRole==='TextInput')?.accessibleValue)||'',searchExpanded:String((els.find(e=>e.accessibleLabel==='Barcode / item search F2')?.accessibleExpanded==='true')||els.filter(e=>e.accessibleRole==='ListItem').length>0),resultCount:els.filter(e=>e.accessibleRole==='ListItem').length,customer:(els.filter(e=>e.accessibleRole==='Combobox').find(e=>!e.accessibleLabel&&e.accessibleValue)?.accessibleValue)||'',warehouse:((els.filter(e=>e.accessibleRole==='Combobox').find(e=>e.accessibleLabel==='Source warehouse')||els.filter(e=>e.accessibleRole==='Combobox').find(e=>e.accessibleLabel==='Shop warehouse'))?.accessibleValue)||'',selectedResultIndex:els.filter(e=>e.accessibleRole==='ListItem').findIndex(e=>e.accessibleItemSelected===true||e.accessibleItemSelected==='true'),results:els.filter(e=>e.accessibleRole==='ListItem').map(e=>(e.accessibleLabel||'').replace(/\s+/g,' ')),firstResult:(els.find(e=>e.accessibleRole==='ListItem')?.accessibleLabel||'').replace(/\s+/g,' '),searchValue:(els.find(e=>e.accessibleLabel==='Barcode / item search F2')?.accessibleValue||'').replace(/[-]/g,'<key>')};if(screen!=='stock-transfer'){delete observed.targetWarehouse;delete observed.transferMode;delete observed.outgoingDisabled;delete observed.outgoingValue}return observed}
const invoiceDetailLabels=['Customer','Customer Name','Posting Date','Posting Time','Payment Due Date','Update Stock','Source Warehouse','Total Quantity','Total (USD)','Grand Total (USD)','In Words','Rounding Adjustment (USD)','Rounded Total (USD)','Total Advance (USD)','Outstanding Amount (USD)'];
function cleanText(value){return String(value||'').replace(/\s+/g,' ').trim()}
async function invoiceDetailNativeObs(ctx){const t=await tree(ctx),els=t.elements||[];const fields={};for(const label of invoiceDetailLabels){const prefix=label+': ';const input=els.find(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel===label);fields[label]=cleanText(input?input.accessibleValue:(els.find(e=>e.accessibleRole==='Text'&&String(e.accessibleLabel||'').startsWith(prefix))?.accessibleLabel||'').slice(prefix.length))}const rowQuery=await queryDescendants(ctx,[{matchElementAccessibleRole:'ListItem'}]);const rows=(Array.isArray(rowQuery)?rowQuery:[]).filter(e=>e.accessibleRole==='ListItem').map(e=>cleanText(e.accessibleLabel));const status=cleanText((els.find(e=>String(e.accessibleLabel||'').startsWith('Status: '))?.accessibleLabel||'').slice(8));const nameCandidates=els.map(e=>cleanText(e.accessibleLabel)).filter(x=>/^ACC-SINV(?:-RET)?-\d{4}-\d+$/.test(x));const actionNames=['Create','Preview','Get Items From','Save','Submit','Cancel'];const actions=actionNames.filter(name=>els.some(e=>e.accessibleRole==='Button'&&e.accessibleLabel===name));const gridActionNames=['Add row','Add multiple','Upload','Delete row','Edit row','Duplicate row'];const gridActions=gridActionNames.filter(name=>els.some(e=>e.accessibleRole==='Button'&&e.accessibleLabel===name));const editableFields={};for(const label of ['Customer','Payment Due Date','Update Stock','Source Warehouse']){const field=els.find(e=>e.accessibleRole==='Text'&&String(e.accessibleLabel||'').startsWith(label+': '));editableFields[label]=field?.accessibleEnabled===true||field?.accessibleEnabled==='true'?'editable':'not-editable'}const editorLabels={'Item code':'itemCode','Item quantity':'qty','Item rate':'rate','Item amount':'amount','Item warehouse':'warehouse'};const itemEditor={open:false};for(const [label,key] of Object.entries(editorLabels)){const input=els.find(e=>e.accessibleRole==='TextInput'&&e.accessibleLabel===label);if(input){itemEditor.open=true;itemEditor[key]=cleanText(input.accessibleValue)}}const validationMessage=cleanText((els.find(e=>String(e.accessibleLabel||'').startsWith('Please fill the following mandatory fields')||/^Row #\d+: Quantity for Item /.test(String(e.accessibleLabel||'')))?.accessibleLabel)||'');const validationTitle=els.some(e=>e.accessibleLabel==='Missing Fields')?'Missing Fields':els.some(e=>e.accessibleLabel==='Invalid Quantity')?'Invalid Quantity':'';const validationState=validationTitle==='Missing Fields'?'missing-fields':validationTitle==='Invalid Quantity'?'invalid-quantity':'none';const permissionState=els.some(e=>String(e.accessibleLabel||'').startsWith('Not Permitted'))?'not-permitted':'permitted';return{title:'Sales Invoice',invoiceName:nameCandidates[0]||'',customerTitle:(els.filter(e=>e.accessibleRole==='Text').map(e=>cleanText(e.accessibleLabel)).find(x=>/^Khit Customer \d+$/.test(x)))||'',status,fields,itemRows:rows,itemCount:rows.length,actions,gridActions,editableFields,itemEditor,permissionState,validationState,validationText:validationMessage}}
async function invoiceDetailWebObs(page){return page.evaluate((labels)=>{const clean=x=>String(x||'').replace(/\s+/g,' ').trim();const get=(field)=>{const roots=[...document.querySelectorAll(`.frappe-control[data-fieldname="${field}"]`)];for(const root of roots){if(root.getAttribute('data-fieldtype')==='Check'){const input=root.querySelector('input[type="checkbox"]');if(input)return input&&(input.checked||input.classList.contains('disabled-selected'))?'checked':'unchecked'}const value=clean(root.querySelector('.control-value')?.innerText||root.querySelector('input')?.value||'');if(value)return value}return''};const specs=[['Customer','customer'],['Customer Name','customer_name'],['Posting Date','posting_date'],['Posting Time','posting_time'],['Payment Due Date','due_date'],['Update Stock','update_stock'],['Source Warehouse','set_warehouse'],['Total Quantity','total_qty'],['Total (USD)','total'],['Grand Total (USD)','grand_total'],['In Words','in_words'],['Rounding Adjustment (USD)','rounding_adjustment'],['Rounded Total (USD)','rounded_total'],['Total Advance (USD)','total_advance'],['Outstanding Amount (USD)','outstanding_amount']];const fields={};for(const [label,field] of specs)fields[label]=clean(get(field));const titleText=clean([...document.querySelectorAll('.page-title')].map(e=>e.textContent).sort((a,b)=>String(b).length-String(a).length)[0]);const statuses=['Credit Note Issued','Partly Paid','Unpaid','Overdue','Paid','Return','Cancelled','Not Saved','Draft'];const status=statuses.find(x=>titleText.endsWith(x))||'';const customerTitle=status?clean(titleText.replace(/^Sales Invoice\s*/,'').slice(0,-status.length)):'';const rows=[...document.querySelectorAll('.frappe-control[data-fieldname="items"] .grid-body .grid-row')].filter(row=>row.getAttribute('data-idx')).map(row=>clean(row.innerText));const actionNames=['Create','Preview','Get Items From','Save','Submit','Cancel'];const visible=[...document.querySelectorAll('button')].filter(button=>button.offsetParent!==null).map(button=>clean(button.innerText));const actions=actionNames.filter(name=>visible.includes(name));const gridActionNames=['Add row','Add multiple','Upload','Delete row','Edit row','Duplicate row'];const gridVisible=[...document.querySelectorAll('.frappe-control[data-fieldname="items"] button')].filter(button=>button.offsetParent!==null).map(button=>clean(button.innerText));const gridActions=gridActionNames.filter(name=>gridVisible.includes(name));const editableFields={};for(const [label,field] of [['Customer','customer'],['Payment Due Date','due_date'],['Update Stock','update_stock'],['Source Warehouse','set_warehouse']]){const input=[...document.querySelectorAll(`.frappe-control[data-fieldname="${field}"]`)].map(root=>root.querySelector('input,textarea')).find(candidate=>candidate&&candidate.offsetParent!==null&&!candidate.closest('.control-input')?.classList.contains('hide'));editableFields[label]=input&&!input.disabled&&!input.readOnly?'editable':'not-editable'}const editor={open:false};const grid=document.querySelector('.frappe-control[data-fieldname="items"]');if(grid){for(const [field,key] of [['item_code','itemCode'],['qty','qty'],['rate','rate'],['amount','amount'],['warehouse','warehouse']]){const input=[...grid.querySelectorAll(`input[data-fieldname="${field}"]`)].find(i=>i.offsetParent!==null);if(input){editor.open=true;editor[key]=clean(input.value)}}}const dialog=[...document.querySelectorAll('.modal,[role="dialog"],.msgprint')].find(e=>e.offsetParent!==null&&/mandatory fields|Missing Fields|cannot be zero|Invalid Quantity/.test(e.innerText||''));const validationState=!dialog?'none':/cannot be zero/.test(dialog.innerText||'')?'invalid-quantity':'missing-fields';const validationText=clean(dialog?.innerText||'');const permissionState=/Not Permitted/.test(document.body.innerText||'')?'not-permitted':'permitted';return{title:'Sales Invoice',invoiceName:decodeURIComponent(location.pathname.split('/').filter(Boolean).pop()||''),customerTitle,status,fields,itemRows:rows,itemCount:rows.length,actions,gridActions,editableFields,itemEditor:editor,permissionState,validationState,validationText}},invoiceDetailLabels)}
async function invoiceWebObs(page){return page.evaluate(()=>{const rows=[...document.querySelectorAll('.list-row')].slice(0,100).map(row=>{const lines=(row.innerText||'').split(String.fromCharCode(10)).map(x=>x.trim()).filter(Boolean);return{customer:lines[0]||'',status:lines[1]||'',postingDate:lines[2]||'',dueDate:lines[3]||'',total:lines[4]||'',id:lines[5]||''}});const body=document.body.innerText||'';const count=(body.match(new RegExp(String.raw`(\d+) of ([\d,+]+)`))||[])[0]||'';return{rows,rowCount:rows.length,countText:count,filterValue:document.querySelector('input[placeholder="ID"]')?.value||'',permissionState:/Not Permitted/.test(body)?'not-permitted':'permitted'}})}
async function invoiceDetailOutcome(page){
 const read=async(voucher)=>page.evaluate(async(voucher)=>{
  const response=await fetch('/api/resource/Sales%20Invoice/'+encodeURIComponent(voucher),{credentials:'include'});
  if(!response.ok)return{error:'HTTP '+response.status};
  const d=(await response.json()).data,item=(d.items||[])[0]||{};
  return{docstatus:d.docstatus,customer:d.customer,dueDate:d.due_date,updateStock:Number(d.update_stock||0),warehouse:d.set_warehouse,totalQty:d.total_qty,grandTotal:d.grand_total,outstanding:d.outstanding_amount,item:{itemCode:item.item_code,qty:item.qty,rate:item.rate,amount:item.amount,warehouse:item.warehouse},items:(d.items||[]).map(row=>({itemCode:row.item_code,qty:row.qty,rate:row.rate,amount:row.amount,warehouse:row.warehouse}))}
 },voucher);
 const web=await read(webInvoice),native=await read(nativeInvoice),keys=['docstatus','customer','dueDate','updateStock','warehouse','totalQty','grandTotal','outstanding'],mismatches=[];
 for(const key of keys)if(JSON.stringify(web[key])!==JSON.stringify(native[key]))mismatches.push({field:key,web:web[key],native:native[key]});
 if(JSON.stringify(web.items)!==JSON.stringify(native.items))mismatches.push({field:'items',web:web.items,native:native.items});
 return{attempted:true,matched:mismatches.length===0,webVoucher:webInvoice,nativeVoucher:nativeInvoice,web,native,mismatches};
}
async function webObs(page){if(screen==='invoice-list')return invoiceWebObs(page);if(screen==='invoice-detail')return invoiceDetailWebObs(page);return page.evaluate(()=>({status:document.querySelector('#status')?.textContent.trim()||'',totalQty:document.querySelector('#total-qty')?.textContent.trim()||'',subtotal:document.querySelector('#subtotal')?.textContent.trim()||'',submitDisabled:Boolean(document.querySelector('#submit-print')?.disabled),lineCount:document.querySelectorAll('#lines-body tr').length,quantities:[...document.querySelectorAll('#lines-body input[aria-label="Quantity"]')].map(x=>x.value),targetWarehouse:document.querySelector('#target-warehouse')?.selectedOptions?.[0]?.textContent?.trim()||'',transferMode:document.querySelector('#transfer-mode')?.selectedOptions?.[0]?.textContent?.trim()||'',outgoingDisabled:Boolean(document.querySelector('#outgoing-stock-entry')?.disabled),outgoingValue:document.querySelector('#outgoing-stock-entry')?.value||'',searchExpanded:document.querySelector('#item-search')?.getAttribute('aria-expanded')||'false',resultCount:document.querySelectorAll('#search-results .search-option-button').length,customer:document.querySelector('#customer')?.selectedOptions?.[0]?.textContent?.trim()||'',warehouse:document.querySelector('#warehouse')?.selectedOptions?.[0]?.textContent?.trim()||'',selectedResultIndex:[...document.querySelectorAll('#search-results .search-option')].findIndex(e=>e.getAttribute('aria-selected')==='true'),results:[...document.querySelectorAll('#search-results .search-option-button')].map(e=>(e.innerText||'').replace(/\s+/g,' ')),firstResult:(document.querySelector('#search-results .search-option-button')?.innerText||'').replace(/\s+/g,' '),searchValue:document.querySelector('#item-search')?.value||''}))}
async function webAct(page,a,steps=[]){
 if(a.op==='wait')return page.waitForTimeout(a.ms||250);
 if(a.op==='key'||a.op==='f2'||a.op==='f9'){await page.keyboard.press(a.op==='f2'?'F2':a.op==='f9'?'F9':a.key);return page.waitForTimeout(250)}
 if(a.op==='search'||a.op==='scan'){await page.locator('#item-search').fill(String(a.value));await page.waitForTimeout(650);if(a.op==='scan'){await page.locator('#item-search').press('Enter');await page.waitForTimeout(400)}return}
 if(a.op==='scroll'){await page.mouse.move(590,344);await page.mouse.wheel(0,Math.abs(Number(a.deltaY||500)));return page.waitForTimeout(350)}
 if(a.op==='increase'||a.op==='decrease'||a.op==='remove'||a.op==='clear'){const label={increase:'Increase quantity',decrease:'Decrease quantity',remove:'Remove line',clear:'Clear'}[a.op];if(a.op==='clear')await page.getByRole('button',{name:'Clear'}).first().click();else await page.getByLabel(label).first().click();return page.waitForTimeout(250)}
 if(a.op==='warehouse'){await page.locator('#warehouse').selectOption({label:String(a.value||'Shop 1')});await page.waitForTimeout(650);return}
 if(a.op==='customer'){await page.locator('#customer').selectOption({label:String(a.value||'Khit Customer 0001')});await page.waitForTimeout(250);return}
 if(a.op==='source-warehouse'){await page.locator('#warehouse').selectOption({label:String(a.value||'Dist Hub 1')});await page.waitForTimeout(250);return}
 if(a.op==='target-warehouse'){await page.locator('#target-warehouse').selectOption({label:String(a.value||'Shop 1')});await page.waitForTimeout(250);return}
 if(a.op==='transfer-mode'){await page.locator('#transfer-mode').selectOption({label:String(a.value||'Direct transfer')});await page.waitForTimeout(250);return}
 if(a.op==='outgoing-entry'){await page.locator('#outgoing-stock-entry').fill(dynamicValue(a,steps,'web'));await page.waitForTimeout(250);return}
 if(a.op==='open-first-invoice'){await page.locator('.list-row .list-subject a[data-doctype="Sales Invoice"]').first().click();await page.waitForTimeout(1800);return}
 if(a.op==='load-more'){await page.getByRole('button',{name:'Load More'}).click();await page.waitForTimeout(1000);return}
 if(a.op==='invoice-sort-toggle'){await page.locator('.sort-selector .btn-order').click();await page.waitForTimeout(1200);return}
 if(a.op==='invoice-filter'){await page.locator('input[placeholder="ID"]').fill(String(a.value||''));await page.waitForTimeout(1000);return}
 if(a.op==='detail-field'){const map={Customer:'customer','Payment Due Date':'due_date','Update Stock':'update_stock','Source Warehouse':'set_warehouse'};const field=map[a.label];if(!field)throw Error('unsupported detail field '+a.label);const control=page.locator(`.frappe-control[data-fieldname="${field}"]`).first();if(field==='update_stock')await control.locator('input[type="checkbox"]').first().click();else{const input=control.locator('input,textarea').first();await input.fill(String(a.value));await input.press('Enter').catch(()=>{});}await page.waitForTimeout(700);return}
 if(a.op==='detail-item-open'){await page.locator('.frappe-control[data-fieldname="items"] .grid-body .grid-row[data-idx="'+String((a.index??0)+1)+'"]').first().click();await page.waitForTimeout(700);return}
 if(a.op==='detail-item-code'){const input=page.locator('.frappe-control[data-fieldname="items"] input[data-fieldname="item_code"]:visible').first();await input.fill(String(a.value));await page.waitForTimeout(850);const option=page.locator('.awesomplete li:visible').filter({hasText:String(a.value)}).first();if(await option.isVisible().catch(()=>false))await option.click();else await input.press('Enter').catch(()=>{});await page.waitForTimeout(1500);return}
 if(a.op==='detail-item-qty-key'||a.op==='detail-item-rate-key'){const field=a.op==='detail-item-qty-key'?'qty':'rate';const input=page.locator('.frappe-control[data-fieldname="items"] input[data-fieldname="'+field+'"]:visible').first();await input.click();await page.keyboard.press(String(a.value));await page.keyboard.press('Enter');await page.waitForTimeout(800);return}
 if(a.op==='detail-item-qty'||a.op==='detail-item-rate'){const field=a.op==='detail-item-qty'?'qty':'rate';const input=page.locator('.frappe-control[data-fieldname="items"] input[data-fieldname="'+field+'"]:visible').first();await input.fill(String(a.value));await input.press('Enter').catch(()=>{});await page.waitForTimeout(700);return}
 if(a.op==='detail-item-add'){await page.locator('.frappe-control[data-fieldname="items"] button:visible').filter({hasText:'Add row'}).first().click();await page.waitForTimeout(700);return}
 if(a.op==='detail-item-select'){await page.locator('.frappe-control[data-fieldname="items"] .grid-row[data-idx="'+String((a.index??0)+1)+'"] input[type="checkbox"]').first().click();await page.waitForTimeout(450);return}
 if(a.op==='detail-item-delete'){await page.locator('.frappe-control[data-fieldname="items"] button:visible').filter({hasText:'Delete row'}).first().click();await page.waitForTimeout(700);return}
 if(a.op==='detail-save'){await page.locator('button:visible').filter({hasText:'Save'}).first().click();await page.waitForTimeout(1800);return}
 if(a.op==='detail-submit'){await page.getByRole('button',{name:'Submit'}).first().click();await page.waitForTimeout(700);const confirm=page.getByRole('button',{name:'Yes'}).first();if(await confirm.isVisible().catch(()=>false))await confirm.click();await page.waitForTimeout(2200);return}
 if(a.op==='quantity'){await page.locator('input[aria-label="Quantity"]').first().fill(String(a.value));await page.waitForTimeout(250);return}
 if(a.op==='choose'){await page.locator('#search-results .search-option-button').nth(a.index||0).click();return page.waitForTimeout(300)}
 throw Error('unsupported web op '+a.op)
}
function permissionStatus(value){const text=String(value);if(/permission/i.test(text)&&/Sales Invoice/i.test(text))return'permission-denied:Sales Invoice';return text}
function compare(a,b){const diffs=[];for(const k of Object.keys(a)){if(screen!=='stock-transfer'&&['targetWarehouse','transferMode','outgoingDisabled','outgoingValue'].includes(k))continue;let x=a[k],y=b[k];if(k==='status'&&scenario==='permission-denied'){x=permissionStatus(x);y=permissionStatus(y)}else if(k==='status'){x=String(x).replace(/[A-Z]+(?:-[A-Z]+)*-\d{4}-\d+/g,'<voucher>');y=String(y).replace(/[A-Z]+(?:-[A-Z]+)*-\d{4}-\d+/g,'<voucher>')}if(k==='outgoingValue'){x=String(x).replace(/[A-Z]+(?:-[A-Z]+)*-\d{4}-\d+/g,'<voucher>');y=String(y).replace(/[A-Z]+(?:-[A-Z]+)*-\d{4}-\d+/g,'<voucher>')}if(k==='firstResult'){x=String(x).replace(/\s+/g,' ');y=String(y).replace(/\s+/g,' ')}if(screen==='invoice-detail'&&k==='invoiceName'){x='<invoice>';y='<invoice>'}if(screen==='invoice-detail'&&k==='itemRows'&&(a.itemEditor?.open||b.itemEditor?.open))continue;if(screen==='invoice-detail'&&k==='fields'){x={...x};y={...y};delete x['Posting Time'];delete y['Posting Time']}if(JSON.stringify(x)!==JSON.stringify(y))diffs.push({field:k,web:x,native:y})}return diffs}
async function businessOutcome(page,steps){
 const final=steps[steps.length-1]||{};
 const extract=(side)=>String(final[side]?.status||'').match(/Submitted\s+([A-Z]+(?:-[A-Z]+)*-\d{4}-\d+)/)?.[1];
 const webVoucher=extract('web'),nativeVoucher=extract('native');
 if(!webVoucher||!nativeVoucher)return{attempted:false,matched:false,reason:'submission voucher number missing',webVoucher,nativeVoucher};
 const transfer=screen==='stock-transfer';
 const read=async(voucher)=>page.evaluate(async({voucher,transfer})=>{
  const doctype=transfer?'Stock%20Entry':'Sales%20Invoice';
  const response=await fetch('/api/resource/'+doctype+'/'+encodeURIComponent(voucher),{credentials:'include'});
  if(!response.ok)return{error:'HTTP '+response.status};
  const d=(await response.json()).data;
  const item=(d.items||[])[0]||{};
  if(transfer)return{docstatus:d.docstatus,fromWarehouse:d.from_warehouse,toWarehouse:d.to_warehouse,addToTransit:Number(d.add_to_transit||0),totalQty:(d.items||[]).reduce((sum,row)=>sum+Number(row.qty||0),0),item:{itemCode:item.item_code,qty:item.qty,uom:item.uom,sourceWarehouse:item.s_warehouse,targetWarehouse:item.t_warehouse}};
  return{docstatus:d.docstatus,customer:d.customer,totalQty:d.total_qty,grandTotal:d.grand_total,outstanding:d.outstanding_amount,item:{itemCode:item.item_code,qty:item.qty,rate:item.rate,amount:item.amount,warehouse:item.warehouse}};
 },{voucher,transfer});
 const web=await read(webVoucher),native=await read(nativeVoucher);
 const keys=transfer?['docstatus','fromWarehouse','toWarehouse','addToTransit','totalQty']:['docstatus','customer','totalQty','grandTotal','outstanding'];
 const mismatches=[];
 for(const key of keys)if(JSON.stringify(web[key])!==JSON.stringify(native[key]))mismatches.push({field:key,web:web[key],native:native[key]});
 for(const key of Object.keys(web.item||{}))if(JSON.stringify(web.item?.[key])!==JSON.stringify(native.item?.[key]))mismatches.push({field:'item.'+key,web:web.item?.[key],native:native.item?.[key]});
 return{attempted:true,matched:mismatches.length===0,webVoucher,nativeVoucher,web,native,mismatches};
}

async function main(){
 const env={...process.env,SLINT_MCP_PORT:String(port),SLINT_EMIT_DEBUG_INFO:'1',KHITMEE_CATALOG_ROWS:'10000',KHITMEE_INVOICE_ROWS:'10000',KHITMEE_ERP_URL:baseUrl,KHITMEE_ERP_USERNAME:username,KHITMEE_ERP_PASSWORD:password,KHITMEE_ERP_WAREHOUSE:'Dist Hub 1 - KMD',KHITMEE_ERP_CUSTOMER:'KM-CUST-0001',KHITMEE_ERP_PRICE_LIST:'Standard Selling',KHITMEE_INITIAL_SCREEN:screen==='invoice-list'?'invoice-list':screen==='invoice-detail'?(scenario==='detail-permission-denied'?'invoice-detail':'invoice-list'):screen==='stock-transfer'?'stock-transfer':'sales',KHITMEE_INVOICE_NAME:nativeInvoice};
 const child=spawn(path.resolve(exe),{cwd:path.resolve('desktop-frontend'),env,stdio:'ignore'});
 try{
  const deadline=Date.now()+30000;
  while(Date.now()<deadline&&!(await nativeReady()))await sleep(100);
  if(!(await nativeReady()))throw Error('native MCP did not start');
  await sleep(1200);
  const launch={headless:true};const devinChrome='C:/devin/chrome/chrome-win64/chrome.exe';if(process.platform==='win32'&&existsSync(devinChrome))launch.executablePath=devinChrome;else if(process.platform==='win32')launch.channel='chrome';const browser=await chromium.launch(launch);
  const page=await (await browser.newContext({viewport:{width:1180,height:760},deviceScaleFactor:1})).newPage();
  if(password){const login=await page.context().request.post(baseUrl+'/api/method/login',{data:{usr:username,pwd:password}});if(!login.ok())throw Error('browser ERP login failed HTTP '+login.status())}
  await page.goto(baseUrl+(screen==='invoice-list'?'/app/sales-invoice':screen==='invoice-detail'?'/app/sales-invoice':screen==='stock-transfer'?'/khitmee_transfer':'/khitmee_sales'));await page.waitForLoadState('networkidle').catch(()=>{});await page.waitForTimeout(screen==='invoice-list'||screen==='invoice-detail'?1800:700);if((screen==='invoice-list'||screen==='invoice-detail')&&await page.locator('.filter-x-button').first().isVisible().catch(()=>false)){await page.locator('.filter-x-button').first().click();await page.waitForTimeout(1000)}
  if(screen==='invoice-list'||screen==='invoice-detail'){const order=page.locator('.sort-selector .btn-order');if(await order.getAttribute('data-value').catch(()=>null)==='asc'){await order.click();await page.waitForTimeout(1000)}}
  const ctx=await nativeContext();if(screen==='invoice-detail'){await page.goto(baseUrl+'/app/sales-invoice/'+encodeURIComponent(webInvoice));await page.waitForLoadState('networkidle').catch(()=>{});await page.waitForTimeout(1800);if(scenario!=='detail-permission-denied'){await nativeAct(ctx,{op:'invoice-filter',value:nativeInvoice},[]);await nativeAct(ctx,{op:'open-first-invoice'},[])}await sleep(1600)}
  const steps=[];const failures=[];
  const initialWeb=await webObs(page),initialNative=await nativeObs(ctx),initialDiffs=compare(initialWeb,initialNative);
  steps.push({index:'initial',action:{op:'initial'},web:initialWeb,native:initialNative,differences:initialDiffs});
  failures.push(...initialDiffs.map(d=>({step:'initial',op:'initial',...d})));
  for(const [i,a] of sequence.entries()){
   await webAct(page,a,steps);await nativeAct(ctx,a,steps);await sleep(200);
   const web=await webObs(page),native=await nativeObs(ctx),diffs=compare(web,native);
   steps.push({index:i,action:a,web,native,differences:diffs});
   failures.push(...diffs.map(d=>({step:i,op:a.op,...d})));
  }
if(screen==='invoice-detail'){
   for(const side of ['web','native']){
    const before=steps.find(s=>s.action?.op==='detail-field')?.[side]?.fields?.['Posting Time'];
    const after=steps.find(s=>s.action?.op==='detail-save')?.[side]?.fields?.['Posting Time'];
    if(before&&after&&after<before)failures.push({step:'businessOutcome',op:'posting-time-progression',field:'fields.Posting Time',web:side==='web'?before:undefined,native:side==='native'?before:undefined,after});
   }
  }
  const businessOutcomeEvidence=screen==='invoice-list'?{attempted:false,matched:true,reason:'read-only invoice journey'}:screen==='invoice-detail'?await invoiceDetailOutcome(page):await businessOutcome(page,steps);
  failures.push(...(businessOutcomeEvidence.mismatches||[]).map(d=>({step:'businessOutcome',op:'erp-readback',...d})));
  await browser.close();
  const report={schemaVersion:1,scenario,passed:failures.length===0,failures,businessOutcome:businessOutcomeEvidence,steps};
  await fs.mkdir(path.dirname(out),{recursive:true});await fs.writeFile(out,JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({passed:report.passed,failureCount:failures.length,out}));
  process.exitCode=report.passed?0:1;
 } finally{child.kill()}
}
main().catch(e=>{console.error(e);process.exitCode=2})
