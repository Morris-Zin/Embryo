#!/usr/bin/env python3
"""Live cache-correctness proof for Item Price create/delete delta synchronization."""
import argparse
import json
import urllib.request
from pathlib import Path
from business_check import login_opener, post_method


def resource(opener, base, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base.rstrip('/') + path.replace(' ', '%20'), data=data, method=method, headers={'content-type': 'application/json'})
    with opener.open(req, timeout=20) as response:
        body = response.read().decode()
        return json.loads(body) if body else {}


def sync(opener, base, token=None):
    status, body = post_method(opener, base, 'khitmee_ops.api.catalog.sync_catalog', {
        'warehouse': 'Dist Hub 1 - KMD',
        'price_list': 'Standard Selling',
        'customer': '',
        'state': token or '',
    })
    if status != 200:
        raise RuntimeError(f'sync_catalog HTTP {status}: {body}')
    return body.get('message') or {}


def rate_for(payload, item_code='KM-00001'):
    for item in payload.get('items') or []:
        if item.get('item_code') == item_code:
            return item.get('rate'), item
    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18086')
    parser.add_argument('--username', default='Administrator')
    parser.add_argument('--password', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    opener = login_opener(args.base_url, args.username, args.password)
    evidence = {'schemaVersion': 1, 'passed': False, 'steps': []}
    temp_name = None
    try:
        initial = sync(opener, args.base_url)
        token = initial.get('sync_token')
        baseline_rate, _ = rate_for(initial)
        evidence['steps'].append({'step': 'initial-reset', 'reset': initial.get('reset'), 'baselineRate': baseline_rate, 'itemCount': len(initial.get('items') or [])})
        created = resource(opener, args.base_url, 'POST', '/api/resource/Item Price', {
            'item_code': 'KM-00001', 'price_list': 'Standard Selling', 'price_list_rate': 99.01,
            'selling': 1, 'buying': 0, 'currency': 'MMK', 'uom': 'Nos',
        })
        temp_name = (created.get('data') or {}).get('name')
        after_create = sync(opener, args.base_url, token)
        create_rate, create_row = rate_for(after_create)
        evidence['steps'].append({'step': 'after-price-create', 'reset': after_create.get('reset'), 'rate': create_rate, 'present': create_row is not None, 'tempPrice': temp_name})
        token = after_create.get('sync_token') or token
        resource(opener, args.base_url, 'DELETE', '/api/resource/Item Price/' + temp_name)
        after_delete = sync(opener, args.base_url, token)
        delete_rate, delete_row = rate_for(after_delete)
        evidence['steps'].append({'step': 'after-price-delete', 'reset': after_delete.get('reset'), 'rate': delete_rate, 'present': delete_row is not None})
        evidence['passed'] = (
            baseline_rate == 18.46 and create_row is not None and create_rate == 99.01
            and delete_row is not None and delete_rate == 18.46 and not after_delete.get('reset')
        )
        evidence['conclusion'] = 'Item Price delete produced a delta refresh restoring the prior authoritative rate' if evidence['passed'] else 'cache correctness mismatch; inspect steps'
    finally:
        if temp_name:
            try:
                resource(opener, args.base_url, 'DELETE', '/api/resource/Item Price/' + temp_name)
            except Exception:
                pass
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': evidence['passed'], 'output': args.output}))
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
