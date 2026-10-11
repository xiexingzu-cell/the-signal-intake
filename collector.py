#!/usr/bin/env python3
"""THE SIGNAL v0.3 RSS intake (preview only; no AI editing or auto-mail).

Standard-library only; deliberately never republishes full articles or third-party images.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
from collections import Counter
from math import ceil
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BEIJING = ZoneInfo('Asia/Shanghai')
ALLOWED_CATEGORIES = {'finance', 'ai', 'quant', 'world'}
TARGET_SHARES = {'finance': .30, 'ai': .30, 'quant': .25, 'world': .15}
MAX_FEED_BYTES = 3_000_000
TRACKING_PARAMS = {'fbclid', 'gclid', 'mc_cid', 'mc_eid', 'igshid', 'ref', 'ref_src'}
LOG = logging.getLogger('signal.collector')


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.muted = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.muted += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.muted = max(0, self.muted - 1)

    def handle_data(self, data):
        if not self.muted:
            self.parts.append(data)


def plain(value, limit=250):
    parser = TextExtractor()
    parser.feed(html.unescape(value or ''))
    text = re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()
    return text[:limit].rstrip() + ('…' if len(text) > limit else '')


def host_allowed(host, whitelist):
    return host.lower() in {str(h).lower() for h in whitelist}


def canonical_url(raw, hosts):
    """Only accept article URLs on the configured publisher hosts."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parts = urlsplit(raw.strip())
        if parts.scheme not in {'http', 'https'} or not parts.hostname or not host_allowed(parts.hostname, hosts):
            return None
        if parts.username or parts.password or parts.port not in {None, 80, 443}:
            return None
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not k.lower().startswith('utm_') and k.lower() not in TRACKING_PARAMS]
        return urlunsplit(('https', parts.hostname.lower(), parts.path or '/', urlencode(q), ''))
    except (ValueError, TypeError):
        return None


def parsed_timestamp(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        return None  # a timezone-free date cannot safely determine recency
    return dt.astimezone(timezone.utc)


def localname(tag):
    return tag.rsplit('}', 1)[-1].lower()


def child_text(el, names):
    for kid in el:
        if localname(kid.tag) in names:
            return ''.join(kid.itertext()).strip()
    return ''


def rss_or_atom(data):
    """Small RSS 2.0 / Atom parser; no network or entity expansion."""
    if len(data) > MAX_FEED_BYTES:
        raise ValueError('Feed exceeds configured 3MB limit')
    root = ET.fromstring(data)
    if localname(root.tag) == 'rss' or localname(root.tag) == 'rdf':
        entries = [e for e in root.iter() if localname(e.tag) == 'item']
    elif localname(root.tag) == 'feed':
        entries = [e for e in root if localname(e.tag) == 'entry']
    else:
        raise ValueError('Not an RSS or Atom feed')
    out = []
    for el in entries[:300]:
        atom = localname(el.tag) == 'entry'
        raw_link = ''
        if atom:
            for k in el:
                if localname(k.tag) == 'link' and k.attrib.get('rel', 'alternate') == 'alternate':
                    raw_link = k.attrib.get('href', '')
                    break
        else:
            raw_link = child_text(el, {'link'})
        pub = child_text(el, {'pubdate', 'published', 'updated', 'date'})
        # Some RSS feeds expose date as a nested element and direct text is not enough.
        title = child_text(el, {'title'})
        summary = child_text(el, {'description', 'summary', 'content', 'encoded'})
        # Extract image candidate only: NOT approved for copying/display.
        image_candidate = None
        for k in el:
            tag = localname(k.tag)
            if tag in {'thumbnail', 'content', 'enclosure'}:
                link = k.attrib.get('url', '')
                typ = k.attrib.get('type', '')
                if link.startswith('https://') and (tag != 'enclosure' or typ.startswith('image/')):
                    image_candidate = link
                    break
        out.append({'title': title, 'summary': summary, 'link': raw_link,
                    'published_raw': pub, 'image_candidate': image_candidate})
    return out


def fetch_feed(url, timeout):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname:
        raise ValueError('Feed URL must use HTTPS')
    request = Request(url, headers={'User-Agent': 'TheSignalPersonalRSS/0.3 (news reader)',
                                    'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml, */*'})
    with urlopen(request, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f'HTTP {r.status}')
        content = r.read(MAX_FEED_BYTES + 1)
    if len(content) > MAX_FEED_BYTES:
        raise ValueError('Feed too large')
    return content


def normalized_title(title):
    return re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', title.casefold())


def provisional_score(entry, source_weight, now):
    """Temporary sorting only; NOT a factual importance judgement."""
    age_hours = max(0, (now - entry['published_dt']).total_seconds() / 3600)
    freshness = max(0, 15 - int(age_hours // 4))
    return min(79, 52 + int(source_weight) + freshness)


def choose_articles(entries, cap):
    """Soft share targets; empty categories never cause fabricated stories."""
    groups = {category: [] for category in ALLOWED_CATEGORIES}
    for entry in entries:
        groups[entry['category']].append(entry)
    for group in groups.values():
        group.sort(key=lambda e: (-e['importance_score'], -e['published_dt'].timestamp(), e['id']))
    quotas = {category: round(cap * TARGET_SHARES[category]) for category in TARGET_SHARES}
    # Make quotas sum exactly cap without changing the target ratios materially.
    while sum(quotas.values()) < cap:
        c = max(quotas, key=lambda k: cap*TARGET_SHARES[k]-quotas[k])
        quotas[c] += 1
    while sum(quotas.values()) > cap:
        c = max(quotas, key=lambda k: quotas[k]-cap*TARGET_SHARES[k])
        quotas[c] -= 1
    selected = []
    used = set()
    selected_by_cat = Counter()
    for cat in TARGET_SHARES:
        for e in groups[cat][:quotas[cat]]:
            if e['id'] not in used:
                selected.append(e)
                used.add(e['id'])
                selected_by_cat[cat] += 1
    # A missing section is a coverage failure, not a license to pad the issue with AI.
    # Never allow one category to exceed 50% of the target capacity.
    category_ceiling = ceil(cap * .50)
    remaining = sorted((e for e in entries if e['id'] not in used),
                       key=lambda e: (-e['importance_score'], -e['published_dt'].timestamp(), e['id']))
    for e in remaining:
        if len(selected) >= cap:
            break
        if selected_by_cat[e['category']] >= category_ceiling:
            continue
        selected.append(e)
        selected_by_cat[e['category']] += 1
    return sorted(selected, key=lambda e: (-e['importance_score'], -e['published_dt'].timestamp()))


def process_feeds(feeds, fetcher, now, hours=48, max_items=24):
    cutoff = now - timedelta(hours=hours)
    failures, counts, accepted = [], Counter(), []
    seen_url, seen_title = set(), set()
    for source in feeds:
        label = source['id']
        try:
            if source['category'] not in ALLOWED_CATEGORIES:
                raise ValueError('unknown category')
            if not source['article_hosts'] or not source['url'].startswith('https://'):
                raise ValueError('bad source configuration')
            source_hours = max(hours, int(source.get('lookback_hours', hours)))
            if source_hours > 336:
                raise ValueError('source lookback_hours exceeds 336')
            source_cutoff = now - timedelta(hours=source_hours)
            items = rss_or_atom(fetcher(source['url']))
            counts[f'{label}:feed_items'] = len(items)
            for item in items:
                title = plain(item['title'], 260)
                url = canonical_url(item['link'], source['article_hosts'])
                pub = parsed_timestamp(item['published_raw'])
                if not title or not url or not pub or not source_cutoff <= pub <= now+timedelta(minutes=15):
                    counts[f'{label}:rejected'] += 1
                    continue
                tkey = normalized_title(title)
                if not tkey or url in seen_url or tkey in seen_title:
                    counts[f'{label}:duplicate'] += 1
                    continue
                seen_url.add(url)
                seen_title.add(tkey)
                article_id = hashlib.sha256(url.encode()).hexdigest()[:16]
                entry = {'id': f'rss-{article_id}', 'category': source['category'],
                         'title': title, 'summary': plain(item['summary'], 210) or '该 RSS 条目未提供摘要，请阅读原文。',
                         'url': url, 'source': source['name'], 'source_id': label,
                         'published_at': pub.isoformat().replace('+00:00','Z'), 'published_dt': pub,
                         'image_candidate': item['image_candidate'],
                         'weekly_research': source['category'] == 'quant' and pub < cutoff,
                         'provenance': 'rss_link_time_verified; body_not_verified'}
                entry['importance_score'] = provisional_score(entry, source.get('weight',0), now)
                accepted.append(entry)
                counts[f'{label}:accepted'] += 1
        except (ValueError, ET.ParseError, HTTPError, URLError, TimeoutError, OSError) as exc:
            msg = f'{label}: {type(exc).__name__}: {str(exc)[:180]}'
            failures.append(msg)
            LOG.warning(msg)
    selected = choose_articles(accepted, max_items)
    missing = [cat for cat in TARGET_SHARES if not any(e['category'] == cat for e in selected)]
    return selected, {'sources_configured': len(feeds), 'sources_failed': failures,
                      'missing_categories': missing,
                      'source_counts': dict(counts), 'total_accepted': len(accepted),
                      'selected': len(selected), 'category_counts': dict(Counter(e['category'] for e in selected)),
                      'period_start': cutoff.isoformat(), 'period_end': now.isoformat()}


def public_issue(entries, now, report):
    count = len(entries)
    articles = []
    for e in entries:
        summary = e['summary']
        weekly = e.get('weekly_research', False)
        if weekly:
            summary = '【近7天量化研究选读，非今日新闻】' + summary
        articles.append({
            'id': e['id'], 'category': e['category'], 'importance_score': e['importance_score'],
            'title': e['title'], 'summary': summary,
            'why_it_matters': ('这是一篇近7天量化论文选读，不代表过去72小时新发生的新闻；原文尚未事实复核。'
                                if weekly else '这条新闻从原始 RSS 收录，尚未经过编辑部事实复核或重要性评级。'),
            'body': [f"发布于 {e['published_at']} · 采集自 {e['source']}。",
                     '这里展示的是 RSS 条目原文摘要，并非新闻全文或独立核实的分析。',
                     summary],
            'sources': [{'name': e['source']+' · 原始报道', 'url': e['url']}],
            'published_at': e['published_at'], 'verification': e['provenance']
        })
    return {
        'issue_number': 'V0.3 · RSS INTAKE PREVIEW',
        'date': now.astimezone(BEIJING).date().isoformat(),
        'notice': (f'真实 RSS 标题初筛预览 · {count} 条 · {len(report["sources_failed"])} 个源采集失败'
                   + (f' · 缺失栏目：{",".join(report.get("missing_categories", []))}' if report.get('missing_categories') else '')
                   + ' · 量化可含近7天研究 · 原文未逐篇核实 · 尚未进行 DeepSeek 中文编辑 · 非正式日报'),
        'markets': [{'label':x,'value':'—','as_of':'行情尚未接入'} for x in
                    ['S&P 500','NASDAQ','USD/CNY','GOLD','WTI OIL','US 10Y']],
        'articles': articles,
        'research_note': {'title':'AQTP 研究观察 · 等待 AI 编辑',
                          'text':'已收录 arXiv 量化论文题录；创新性、可复现性和 AQTP 价值尚未核实。'},
        'tracking': {'intro':'真实重大事件追踪将在多日数据积累并完成同一事件归并后开启。','events': []},
        'stage': 'rss_collected_not_published',
        'generated_at': now.isoformat()
    }


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
    tmp.replace(path)


def load_feeds(path):
    feeds = json.loads(path.read_text('utf-8'))
    if not isinstance(feeds, list) or not feeds:
        raise ValueError('feeds.json empty or invalid')
    ids = set()
    for f in feeds:
        if f['id'] in ids or f['category'] not in ALLOWED_CATEGORIES:
            raise ValueError('duplicate id or invalid category')
        ids.add(f['id'])
    return feeds


def main(argv=None):
    parser = argparse.ArgumentParser(description='THE SIGNAL v0.3: RSS discovery and provenance report')
    parser.add_argument('--hours', type=int, default=48, help='time window in hours (default 48)')
    parser.add_argument('--max-items', type=int, default=24)
    parser.add_argument('--timeout', type=int, default=12)
    parser.add_argument('--preview', action='store_true', help='also replace only site/v03/news.json')
    parser.add_argument('--as-of', help='ISO timestamp for deterministic offline tests')
    parser.add_argument('--fixture-dir', type=Path, help='test-only: use named local XML files instead of network')
    args = parser.parse_args(argv)
    if not 1 <= args.hours <= 336 or not 1 <= args.max_items <= 100 or not 1 <= args.timeout <= 60:
        parser.error('hours/max-items/timeout out of range')
    now = parsed_timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if now is None:
        parser.error('Invalid --as-of; use e.g. 2026-10-10T08:00:00+08:00')
    feeds = [f for f in load_feeds(ROOT / 'feeds.json') if f.get('enabled', True)]
    if args.fixture_dir:
        LOG.warning('OFFLINE FIXTURE MODE: results are NOT real live news')
        mapped = {f['url']: args.fixture_dir / f"{f['id']}.xml" for f in feeds}
        def reader(url):
            return mapped[url].read_bytes()
    else:
        def reader(url):
            return fetch_feed(url, args.timeout)
    entries, report = process_feeds(feeds, reader, now, args.hours, args.max_items)
    if args.fixture_dir:
        report['test_mode'] = True
    else:
        report['test_mode'] = False
    print('===== THE SIGNAL V0.3 RSS INTAKE =====')
    print('近',args.hours,'小时 · 候选',report['total_accepted'],'· 入选',report['selected'])
    print('版块：',report['category_counts'])
    print('缺失版块：', ', '.join(report['missing_categories']) or '无')
    print('各来源明细：')
    for f in feeds:
        sid=f['id']
        print(f'  - {sid}: 条目={report["source_counts"].get(sid+":feed_items",0)} / 合格={report["source_counts"].get(sid+":accepted",0)} / 过滤={report["source_counts"].get(sid+":rejected",0)}')
    print('信源失败：',len(report['sources_failed']))
    for error in report['sources_failed']:
        print('  -', error)
    if not entries:
        print('没有通过时间、链接和来源检查的内容；未覆盖网站数据。', file=sys.stderr)
        return 2
    output = ROOT / 'issues' / ('candidates-' + now.astimezone(BEIJING).strftime('%Y%m%d-%H%M%S') + '.json')
    report_path = output.with_name(output.stem + '.report.json')
    # Keep raw candidates (including image *candidate* only) private inside the working directory.
    atomic_json(output, {'generated_at': now.isoformat(),
                         'entries': [{k:v for k,v in e.items() if k!='published_dt'} for e in entries]})
    atomic_json(report_path, report)
    print('候选数据：',output)
    print('诊断报告：',report_path)
    if args.preview:
        if args.fixture_dir:
            print('离线测试模式禁止发布到网页。',file=sys.stderr)
            return 3
        site_path = ROOT.parent / 'site' / ROOT.name / 'news.json'
        atomic_json(site_path, public_issue(entries, now, report))
        print('预览文件：',site_path)
    print('提示：这是 RSS 题录初筛，不是新闻事实核验或正式出版。')
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    raise SystemExit(main())
