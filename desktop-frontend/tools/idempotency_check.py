#!/usr/bin/env python3
"""Live ERP proof that concurrent duplicate sale submission replays one authoritative voucher."""
import argparse,json,threading,uuid
from pathlib import Path
from business_check import login_opener,post_method

def main():
 p=argparse.ArgumentParser();p.add_argument('--base-url',default='http://127.0.0.1:18086');p.add_argument('--username',default='Administrator');p.add_argument('--password',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 client_id='native-proof-'+uuid.uuid4().hex
 payload={'client_voucher_id':client_id,'warehouse':'Dist Hub 1 - KMD','customer':'KM-CUST-0001','price_list':'Standard Selling','items':[{'item_code':'KM-00001','qty':1,'uom':'Nos'}],'printer_type':'voucher'}
 opener=login_opener(a.base_url,a.username,a.password)
 results=[None,None]
 def submit(index):
  results[index]=post_method(opener,a.base_url,'khitmee_ops.api.vouchers.submit_sale',payload)
 threads=[threading.Thread(target=submit,args=(i,)) for i in range(2)]
 for t in threads:t.start()
 for t in threads:t.join()
 statuses=[r[0] for r in results]
 bodies=[r[1] for r in results]
 vouchers=[]
 for status,body in zip(statuses,bodies):
  voucher=(body.get('message') or {}).get('voucher_no') if isinstance(body,dict) else None
  if status==200 and voucher:vouchers.append(voucher)
 # If a concurrent loser received the transient busy error, retry once after the winner committed.
 if len(set(vouchers))<1 or len(vouchers)<2:
  status,body=post_method(opener,a.base_url,'khitmee_ops.api.vouchers.submit_sale',payload)
  voucher=(body.get('message') or {}).get('voucher_no') if isinstance(body,dict) else None
  if status==200 and voucher:vouchers.append(voucher)
 distinct=sorted(set(vouchers))
 passed=len(vouchers)>=2 and len(distinct)==1
 report={'schemaVersion':1,'passed':passed,'clientVoucherId':client_id,'initialStatuses':statuses,'voucherAttempts':vouchers,'distinctVouchers':distinct,'oracle':'all concurrent/replayed attempts for one client_voucher_id must resolve to the same Sales Invoice'}
 out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({'passed':passed,'distinctVouchers':distinct,'output':str(out)}))
 return 0 if passed else 1
if __name__=='__main__':raise SystemExit(main())
