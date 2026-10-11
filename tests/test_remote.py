import sys
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from collect_remote import build_snapshot

class RemoteTests(unittest.TestCase):
    def test_snapshot(self):
        feed = [{'id':'world_demo','name':'World','category':'world','url':'https://feed.test/rss',
                 'article_hosts':['news.test'],'weight':3}]
        now=datetime(2026,10,11,0,0,tzinfo=timezone.utc)
        xml=b'''<rss><channel><item><title>World event</title><link>https://news.test/story</link>
        <pubDate>Sun, 11 Oct 2026 00:00:00 GMT</pubDate><description>Summary here.</description></item></channel></rss>'''
        obj,report=build_snapshot(feed,lambda url: xml,now)
        self.assertEqual(obj['schema'],'the-signal.remote.v1')
        self.assertEqual(len(obj['entries']),1)
        self.assertNotIn('published_dt',obj['entries'][0])
        self.assertEqual(report['category_counts']['world'],1)

    def test_empty_does_not_overwrite(self):
        with self.assertRaises(ValueError):
            build_snapshot([{'id':'a','name':'x','category':'ai','url':'https://feed.test/rss',
                           'article_hosts':['news.test'],'weight':3}],lambda _: b'<rss><channel/></rss>',
                           datetime(2026,10,11,tzinfo=timezone.utc))
