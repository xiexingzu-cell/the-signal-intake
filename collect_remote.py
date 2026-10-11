#!/usr/bin/env python3
"""GitHub Actions RSS candidate intake, metadata only, no full articles or images."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from collector import load_feeds, process_feeds, fetch_feed, atomic_json

ROOT = Path(__file__).resolve().parent

def build_snapshot(feeds, fetcher, now, hours=72, cap=100):
    entries, report = process_feeds(feeds, fetcher, now, hours=hours, max_items=cap)
    if not entries:
        raise ValueError('No accepted articles: retain previous snapshot')
    snapshot = {
        'schema': 'the-signal.remote.v1',
        'generated_at': now.isoformat().replace('+00:00','Z'),
        'provider': 'github_actions_rss',
        'entries': [{k: v for k, v in e.items() if k != 'published_dt'} for e in entries],
    }
    report.update({'schema': snapshot['schema'], 'generated_at': snapshot['generated_at'],
                   'snapshot_entries': len(entries), 'preview_only': True})
    return snapshot, report

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours',type=int,default=72)
    ap.add_argument('--max-items',type=int,default=100)
    ap.add_argument('--timeout',type=int,default=12)
    args = ap.parse_args()
    if not 1 <= args.hours <= 336 or not 1 <= args.max_items <= 100 or not 1 <= args.timeout <= 60:
        ap.error('parameter out of range')
    feeds = [f for f in load_feeds(ROOT/'feeds.json') if f.get('enabled',True)]
    now = datetime.now(timezone.utc)
    snapshot, report = build_snapshot(feeds, lambda u: fetch_feed(u,args.timeout), now,
                                      hours=args.hours, cap=args.max_items)
    atomic_json(ROOT/'snapshots/latest.json',snapshot)
    atomic_json(ROOT/'snapshots/latest.report.json', report)
    print('THE SIGNAL overseas snapshot: entries=',len(snapshot['entries']))
    print('Category counts:',report['category_counts'])
    print('Missing:',report['missing_categories'])
    print('Failed:',len(report['sources_failed']))
    for f in report['sources_failed']:
        print('  ',f)
    return 0

if __name__ == '__main__':
    sys.exit(main())
