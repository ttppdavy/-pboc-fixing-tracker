import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

P = Path(__file__).resolve().parents[1] / "scripts" / "update_data.py"
spec = importlib.util.spec_from_file_location("tracker", P)
tracker = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = tracker
spec.loader.exec_module(tracker)


def test_forecast_regex():
    s = "PBOC is expected to set the USD/CNY reference rate at 6.7706 – Reuters estimate"
    assert float(tracker.FORECAST_RE.search(s).group(1)) == 6.7706


def test_actual_regex():
    s = "PBOC sets USD/ CNY central rate at 6.7917 (vs. estimate at 6.7706)"
    m = tracker.ACTUAL_RE.search(s)
    assert (float(m.group(1)), float(m.group(2))) == (6.7917, 6.7706)


def test_deviation():
    old = {}
    est = {"2026-07-21": {"reuters_estimate": 6.7706}}
    official = {"2026-07-21": 6.7917}
    row = tracker.merge_rows(old, est, official)["2026-07-21"]
    assert row.deviation_points == 211


def test_actual_regex_variants():
    samples = [
        "PBOC sets USD/ CNY reference rate for today at 6.7909 (vs. estimate at 6.7577)",
        "PBOC sets USD/ CNY mid-point today at 6.7910 (vs. estimate at 6.7965)",
        "PBOC set USD/CNY central rate at 7.1020 (vs estimate at 7.1100)",
    ]
    assert all(tracker.ACTUAL_RE.search(x) for x in samples)


def test_fresh_listing_request_bypasses_cache():
    class Response:
        status_code = 200
        text = "<html></html>"

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self):
            self.url = ""
            self.headers = {}

        def get(self, url, *, headers, timeout):
            self.url = url
            self.headers = headers
            return Response()

    session = Session()
    tracker.get_text(session, "https://investinglive.com/Tag/cny/", fresh=True)
    assert "_refresh" in parse_qs(urlsplit(session.url).query)
    assert session.headers["Cache-Control"] == "no-cache"
    assert session.headers["Pragma"] == "no-cache"


def test_homepage_relative_time_uses_article_metadata():
    forecast_url = (
        "https://investinglive.com/central-banks/"
        "pboc-is-expected-to-set-the-usd-cny-reference-rate-at-6-7737-reuters-estimate/"
    )
    listing = f'''<html><body><a href="{forecast_url}">
        PBOC is expected to set the USD/CNY reference rate at 6.7737 – Reuters estimate
    </a><span>8 hours ago</span></body></html>'''
    article = '''<html><head><meta property="article:published_time"
        content="2026-07-22T00:33:41Z"></head></html>'''

    class Response:
        status_code = 200

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    class Session:
        def get(self, url, *, headers, timeout):
            return Response(article if "pboc-is-expected" in url else listing)

    articles, _ = tracker.extract_articles(Session(), "https://investinglive.com/", fresh=True)
    assert articles == [{
        "date": "2026-07-22",
        "title": "PBOC is expected to set the USD/CNY reference rate at 6.7737 – Reuters estimate",
        "url": forecast_url,
    }]


def test_homepage_streamed_payload_discovers_forecast():
    payload = r'''<script>self.__next_f.push([1,"{\"contentType\":\"Article\",
        \"displayText\":\"PBOC is expected to set the USD/CNY reference rate at 6.7737 – Reuters estimate\",
        \"path\":\"central-banks/pboc-is-expected-to-set-the-usd-cny-reference-rate-at-6-7737-reuters-estimate\",
        \"published\":true,\"latest\":true,
        \"publishedUtc\":\"2026-07-22T00:33:41.6543252Z\"}"])</script>'''
    assert tracker.extract_embedded_articles(payload, "https://investinglive.com/") == [{
        "date": "2026-07-22",
        "title": "PBOC is expected to set the USD/CNY reference rate at 6.7737 – Reuters estimate",
        "url": (
            "https://investinglive.com/central-banks/"
            "pboc-is-expected-to-set-the-usd-cny-reference-rate-at-6-7737-reuters-estimate"
        ),
    }]


def test_streamed_article_path_is_site_root_relative():
    payload = r'''\"displayText\":\"PBOC is expected to set the USD/CNY reference rate at 6.7737 – Reuters estimate\",
        \"path\":\"central-banks/pboc-is-expected-to-set-the-usd-cny-reference-rate-at-6-7737-reuters-estimate\",
        \"publishedUtc\":\"2026-07-22T00:33:41Z\"'''
    article = tracker.extract_embedded_articles(
        payload, "https://investinglive.com/Tag/pboc/"
    )[0]
    assert article["url"].startswith("https://investinglive.com/central-banks/")
