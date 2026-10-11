import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'collector.py'
spec = importlib.util.spec_from_file_location('collector', MODULE)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

NOW = datetime(2026, 10, 10, 8, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)


def rss(xml_items):
    return ('<rss version="2.0"><channel><title>Test</title>' + ''.join(xml_items) + '</channel></rss>').encode()


def item(title, url, dt, summary='Small &amp; accurate description'):
    return '<item><title>'+title+'</title><link>'+url+'</link><description>'+summary+'</description><pubDate>'+dt+'</pubDate></item>'


class Tests(unittest.TestCase):
    def test_url_whitelist(self):
        self.assertIsNone(collector.canonical_url('https://evil.example/hijack', ['bbc.com']))
        self.assertIsNone(collector.canonical_url('file:///etc/passwd', ['bbc.com']))
        self.assertIsNone(collector.canonical_url('https://bbc.com.evil.example/s', ['bbc.com']))
        self.assertIsNone(collector.canonical_url('https://user:pass@bbc.com/a', ['bbc.com']))
        self.assertEqual(collector.canonical_url('http://bbc.com/a?utm_source=a&ref=foo&id=1#news', ['bbc.com']),
                         'https://bbc.com/a?id=1')

    def test_timezones(self):
        self.assertEqual(collector.parsed_timestamp('Sat, 10 Oct 2026 00:00:00 GMT'), NOW)
        self.assertEqual(collector.parsed_timestamp('2026-10-10T08:00:00+08:00'), NOW)
        self.assertIsNone(collector.parsed_timestamp('2026-10-10T08:00:00'))

    def test_rss_atom_and_media(self):
        rss_input = b'''<rss xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/" version="2.0"><channel>
        <item><title>Fed Statement</title><link>https://www.federalreserve.gov/newsevents/pressreleases/a.htm</link>
        <description><![CDATA[<p>Short <b>description</b></p>]]></description><dc:date>2026-10-10T00:00:00Z</dc:date>
        <media:content url="https://www.federalreserve.gov/image.jpg" type="image/jpeg"/></item></channel></rss>'''
        one = collector.rss_or_atom(rss_input)[0]
        self.assertEqual(one['published_raw'], '2026-10-10T00:00:00Z')
        self.assertEqual(collector.plain(one['summary']), 'Short description')
        self.assertEqual(one['image_candidate'], 'https://www.federalreserve.gov/image.jpg')
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Study</title><link href="https://arxiv.org/abs/2601.00123" />
        <updated>2026-10-10T00:00:00Z</updated><summary>A paper</summary></entry></feed>'''
        self.assertEqual(collector.rss_or_atom(atom)[0]['link'], 'https://arxiv.org/abs/2601.00123')

    def test_filters_and_dedup(self):
        source = {'id':'one','name':'Official','url':'https://feed.example/rss','article_hosts':['bbc.com'],
                  'category':'world','weight':5}
        today = 'Sat, 10 Oct 2026 00:00:00 GMT'
        lastyear = 'Fri, 10 Oct 2025 00:00:00 GMT'
        xml = rss([item('Same story','https://bbc.com/story?utm_campaign=test',today),
                   item('Same story','https://bbc.com/other',today),
                   item('Malicious','https://evil.example/a',today),
                   item('Historical','https://bbc.com/old',lastyear),
                   item('No time','https://bbc.com/undated','')])
        selected, report = collector.process_feeds([source],lambda _:xml,NOW,48,20)
        self.assertEqual(len(selected),1)
        self.assertEqual(report['total_accepted'],1)
        self.assertEqual(report['source_counts']['one:duplicate'],1)
        self.assertEqual(report['source_counts']['one:rejected'],3)
        issue = collector.public_issue(selected,NOW,report)
        self.assertEqual(issue['articles'][0]['sources'][0]['url'],'https://bbc.com/story')
        self.assertNotIn('image',issue['articles'][0])
        self.assertIn('非正式日报',issue['notice'])

    def test_empty_feed_and_partial_failure(self):
        a = {'id':'a','name':'A','category':'finance','url':'https://feed.example/a',
             'article_hosts':['bbc.com'],'weight':5}
        b = dict(a,id='b',url='https://feed.example/b')
        xml = rss([item('One','https://bbc.com/news-one','Sat, 10 Oct 2026 00:00:00 GMT')])
        def fetch(url):
            if url.endswith('/b'): raise OSError('Feed blocked')
            return xml
        data, report = collector.process_feeds([a,b],fetch,NOW,48,24)
        self.assertEqual(len(data),1)
        self.assertEqual(len(report['sources_failed']),1)
        self.assertEqual(report['category_counts']['finance'],1)

    def test_soft_quota_does_not_overfill_ai(self):
        entries=[]
        for i in range(30):
            entries.append({'id':f'ai{i}', 'category':'ai','importance_score':70,
                            'published_dt':NOW})
        chosen=collector.choose_articles(entries,24)
        self.assertLessEqual(len(chosen),12)
        self.assertTrue(all(e['category']=='ai' for e in chosen))

    def test_quant_weekly_research_window(self):
        src={'id':'quant_week','name':'arXiv research','url':'https://feed.example/rss',
             'article_hosts':['arxiv.org'],'category':'quant','weight':5,'lookback_hours':168}
        five_days_ago=(NOW-timedelta(days=5))
        from email.utils import format_datetime
        xml=rss([item('Research on quant factors','https://arxiv.org/abs/2601.23456',format_datetime(five_days_ago))])
        selected, report=collector.process_feeds([src],lambda _:xml,NOW,72,24)
        self.assertEqual(report['category_counts'].get('quant'),1)
        self.assertTrue(selected[0]['weekly_research'])
        issue=collector.public_issue(selected,NOW,report)
        self.assertIn('近7天',issue['articles'][0]['summary'])

    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'x.json'
            collector.atomic_json(p, {'ok':True})
            self.assertTrue(json.loads(p.read_text())['ok'])
            self.assertFalse(p.with_name('x.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
