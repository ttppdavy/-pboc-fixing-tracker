import importlib.util
import json
import subprocess
from datetime import date
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
    est = {"2026-07-21": {"actual_article_estimate": 6.7706}}
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


def test_fxstreet_listing_and_article_description():
    article_url = (
        "https://www.fxstreet.com/news/"
        "pboc-sets-usd-cny-reference-rate-at-67939-vs-67906-previous-202607240115"
    )
    listing = f'''<html><body>
        <a href="{article_url}">
          PBOC sets USD/CNY reference rate at 6.7939 vs. 6.7906 previous
        </a>
    </body></html>'''
    assert tracker.extract_fxstreet_listing(
        listing, "https://www.fxstreet.com/search?q=PBOC"
    ) == [{
        "date": "2026-07-24",
        "actual": 6.7939,
        "title": "PBOC sets USD/CNY reference rate at 6.7939 vs. 6.7906 previous",
        "url": article_url,
    }]

    article = '''<html><head><meta name="description"
        content="The PBOC sets the rate at 6.7939, compared with the previous
        fix and 6.7795 Reuters estimate."></head></html>'''
    assert tracker.extract_fxstreet_estimate(article) == 6.7795
    no_estimate = '''<html><head><meta name="description"
        content="The PBOC sets the rate at 6.7928 compared with the previous
        fixing of 6.7911."></head></html>'''
    assert tracker.extract_fxstreet_estimate(no_estimate) is None


def test_fxstreet_fallback_requires_matching_official_actual():
    fxstreet = {
        "2026-07-24": {
            "fxstreet_estimate": 6.7795,
            "fxstreet_actual": 6.7939,
            "fxstreet_url": "https://www.fxstreet.com/news/example",
        }
    }
    row = tracker.merge_rows(
        {},
        {},
        {"2026-07-24": 6.7939},
        fxstreet,
    )["2026-07-24"]
    assert row.reuters_estimate == 6.7795
    assert row.deviation_points == 144
    assert row.forecast_url == "https://www.fxstreet.com/news/example"
    assert row.quality_note == (
        "investinglive_estimate_missing; using FXStreet Reuters estimate"
    )

    rejected = tracker.merge_rows(
        {},
        {},
        {"2026-07-24": 6.8000},
        fxstreet,
    )["2026-07-24"]
    assert rejected.reuters_estimate is None


def test_investinglive_published_estimate_wins_over_fxstreet():
    investinglive = {
        "2026-07-24": {
            "actual_article_estimate": 6.7796,
            "actual_url": "https://investinglive.com/example",
        }
    }
    fxstreet = {
        "2026-07-24": {
            "fxstreet_estimate": 6.7795,
            "fxstreet_actual": 6.7939,
            "fxstreet_url": "https://www.fxstreet.com/news/example",
        }
    }
    row = tracker.merge_rows(
        {},
        investinglive,
        {"2026-07-24": 6.7939},
        fxstreet,
    )["2026-07-24"]
    assert row.reuters_estimate == 6.7796
    assert row.forecast_url == "https://investinglive.com/example"
    assert row.quality_note == ""


def test_chinamoney_curl_uses_browser_headers(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        payload = {
            "head": {"rep_code": "200"},
            "data": {"pageTotal": 1},
            "records": [{"date": "2026-08-25", "values": ["6.7852"]}],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(tracker.subprocess, "run", fake_run)
    result = tracker.fetch_chinamoney(date(2026, 8, 25), date(2026, 8, 25))

    assert result == {"2026-08-25": 6.7852}
    command, kwargs = calls[0]
    joined = " ".join(command)
    assert "--fail-with-body" in command
    assert "User-Agent:" in joined
    assert "Referer: https://www.chinamoney.com.cn/chinese/bkccpr/" in joined
    assert "Accept: application/json, text/plain, */*" in joined
    assert kwargs == {"check": True, "capture_output": True, "text": True}


def test_chinamoney_failure_continues_with_empty_result(monkeypatch, caplog):
    def fail(_start, _end):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(tracker, "fetch_chinamoney", fail)
    result = tracker.fetch_chinamoney_safe(
        date(2026, 8, 25), date(2026, 8, 25)
    )

    assert result == {}
    assert "continuing with published article fallbacks" in caplog.text


def test_investinglive_actual_is_used_when_chinamoney_is_unavailable():
    estimates = {
        "2026-08-25": {
            "actual_article_estimate": 6.7219,
            "investinglive_actual": 6.7852,
            "actual_url": "https://investinglive.com/example",
        }
    }
    row = tracker.merge_rows({}, estimates, {})["2026-08-25"]

    assert row.reuters_estimate == 6.7219
    assert row.official_fix == 6.7852
    assert row.deviation_points == 633
    assert row.actual_source == "investinglive_fallback"
    assert row.quality_note == (
        "official_api_missing; using published actual article"
    )
