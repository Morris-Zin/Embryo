#!/usr/bin/env python3
"""Summarize a Chrome trace without loading the full trace into memory."""
import argparse
import collections
import gzip
import json
import sys


def open_trace(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8')
    return open(path, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('trace')
    parser.add_argument('--top', type=int, default=20)
    args = parser.parse_args()
    events = collections.Counter()
    durations = collections.Counter()
    maximum = collections.Counter()
    functions = collections.Counter()
    function_counts = collections.Counter()
    total_x = 0
    with open_trace(args.trace) as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith('{'):
                continue
            if line.endswith(','):
                line = line[:-1]
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get('name', '')
            events[name] += 1
            if event.get('ph') != 'X':
                continue
            dur_ms = event.get('dur', 0) / 1000
            total_x += dur_ms
            durations[name] += dur_ms
            maximum[name] = max(maximum[name], dur_ms)
            data = event.get('args', {}).get('data', {})
            if name in {'FunctionCall', 'v8.callFunction'}:
                fn = data.get('functionName') or '(anonymous)'
                url = data.get('url') or '(inline)'
                key = f'{fn} {url}'
                functions[key] += dur_ms
                function_counts[key] += 1
    print(f'trace={args.trace} total_complete_event_ms={total_x:.2f}')
    print('\nTop events by total duration:')
    for name, value in durations.most_common(args.top):
        print(f'{value:10.2f} ms  x{events[name]:<7} max={maximum[name]:8.2f} ms  {name}')
    if functions:
        print('\nTop JS function calls by total duration:')
        for key, value in functions.most_common(args.top):
            print(f'{value:10.2f} ms  x{function_counts[key]:<7} {key}')

if __name__ == '__main__':
    main()
