#!/usr/bin/env python3
"""Backfill and update Reuters-estimate vs official PBOC USD/CNY fixing data."""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import random
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "pboc_fixing.csv"
DASHBOARD_FILE = ROOT / "docs" / "index.html"
JSON_FILE = ROOT / "docs" / "data.json"
TZ = ZoneInfo("Asia/Shanghai")

FORECAST_RE = re.compile(
    r"PBOC\s+is\s+expected\s+to\s+set\s+the\s+USD\s*/?\s*CNY\s+reference\s+rate\s+at\s+([0-9]+(?:\.[0-9]+)?)",
    re.I,
)
ACTUAL_RE = re.compile(
    r"PBOC.*?(?:sets?|set).*?USD\s*/?\s*CNY.*?(?:at|today\s+at)\s+([0-9]+(?:\.[0-9]+)?)\s*\(\s*vs\.?\s*estimate\s+at\s+([0-9]+(?:\.[0-9]+)?)",
    re.I,
)
DATE_TEXT_RE = re.compile(r"\b([0-3]\d)/([01]\d)/(20\d{2})\b")
DATE_URL_RE = re.compile(r"(?<!\d)(20\d{6})(?!\d)")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
]

@dataclass
class Row:
    date: str
    reuters_estimate: float | None = None
    official_fix: float | None = None
    deviation_points: int | None = None
    forecast_url: str = ""
    official_url: str = "https://www.chinamoney.com.cn/chinese/bkccpr/"
    actual_source: str = ""
    quality_note: str = ""
    retrieved_at: str = ""


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept-Language": "en-US,en;q=0.8", "Connection": "keep-alive"})
    return s


def get_text(s: requests.Session, url: str, *, attempts: int = 4, timeout: int = 25) -> str:
    last: Exception | None = None
    for n in range(attempts):
        try:
            headers = {"User-Agent": random.choice(USER_AGENTS)}
            r = s.get(url, headers=headers, timeout=timeout)
            if r.status_code in (404, 410):
                return ""
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(8, 1.5 * (n + 1)))
    raise RuntimeError(f"fetch failed: {url}: {last}")


def parse_date_from_href(href: str) -> str | None:
    m = DATE_URL_RE.search(href)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def parse_date_near_anchor(anchor) -> str | None:
    node = anchor
    for _ in range(7):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = " ".join(node.stripped_strings)
        m = DATE_TEXT_RE.search(text)
        if m:
            dd, mm, yyyy = m.groups()
            return date(int(yyyy), int(mm), int(dd)).isoformat()
    return None


def parse_article_date(s: requests.Session, url: str) -> str | None:
    text = get_text(s, url)
    if not text:
        return None
    soup = BeautifulSoup(text, "lxml")
    for key, attr in (("property", "article:published_time"), ("name", "date"), ("itemprop", "datePublished")):
        tag = soup.find("meta", attrs={key: attr})
        if tag and tag.get("content"):
            try:
                return datetime.fromisoformat(tag["content"].replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                pass
    time_tag = soup.find("time")
    if time_tag:
        raw = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw[:10], fmt).date().isoformat()
            except ValueError:
                continue
    m = DATE_TEXT_RE.search(soup.get_text(" ", strip=True))
    if m:
        dd, mm, yyyy = m.groups()
        return date(int(yyyy), int(mm), int(dd)).isoformat()
    return None


def extract_articles(s: requests.Session, page_url: str) -> tuple[list[dict], bool]:
    text = get_text(s, page_url)
    if not text:
        return [], False
    soup = BeautifulSoup(text, "lxml")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for a in soup.find_all("a", href=True):
        title = " ".join(a.stripped_strings)
        if not title or not (FORECAST_RE.search(title) or ACTUAL_RE.search(title)):
            continue
        href = urljoin(page_url, a["href"])
        key = (title, href)
        if key in seen:
            continue
        seen.add(key)
        d = parse_date_from_href(href) or parse_date_near_anchor(a)
        if d is None:
            try:
                d = parse_article_date(s, href)
            except Exception as exc:  # noqa: BLE001
                logging.warning("article date failed %s: %s", href, exc)
        if d:
            out.append({"date": d, "title": title, "url": href})
    has_next = bool(soup.find("a", string=re.compile(r"^\s*Next\s*$", re.I)))
    return out, has_next


def crawl_investinglive(start: date, *, full: bool) -> dict[str, dict]:
    s = session()
    # Both tags are used because the site changed taxonomy and pagination during the history.
    bases = ["https://investinglive.com/Tag/cny", "https://investinglive.com/Tag/pboc"]
    max_pages = 260 if full else 8
    records: dict[str, dict] = {}
    for base in bases:
        empty_run = 0
        for page in range(1, max_pages + 1):
            url = f"{base}/" if page == 1 else f"{base}/page/{page}/"
            try:
                articles, has_next = extract_articles(s, url)
            except Exception as exc:  # noqa: BLE001
                logging.warning("tag page failed %s: %s", url, exc)
                empty_run += 1
                if empty_run >= 8:
                    break
                continue
            if not articles:
                empty_run += 1
            else:
                empty_run = 0
            for item in articles:
                if item["date"] < start.isoformat():
                    continue
                rec = records.setdefault(item["date"], {})
                fm = FORECAST_RE.search(item["title"])
                if fm:
                    # The dedicated forecast article is authoritative for the Reuters estimate.
                    rec["reuters_estimate"] = float(fm.group(1))
                    rec["forecast_url"] = item["url"]
                am = ACTUAL_RE.search(item["title"])
                if am:
                    rec["investinglive_actual"] = float(am.group(1))
                    rec["actual_article_estimate"] = float(am.group(2))
                    rec["actual_url"] = item["url"]
            if page % 10 == 0:
                logging.info("%s: scanned %s pages, %s dates", base, page, len(records))
            if empty_run >= 8 or (not has_next and page > 20 and not full):
                break
            time.sleep(0.15)
    return records


def date_chunks(start: date, end: date, days: int = 330) -> Iterable[tuple[date, date]]:
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=days - 1))
        yield cur, stop
        cur = stop + timedelta(days=1)


def fetch_chinamoney(start: date, end: date) -> dict[str, float]:
    """Fetch official USD/CNY fixing history from ChinaMoney.

    ChinaMoney currently rejects the TLS fingerprint used by Python requests
    with HTTP 403, and its endpoint rejects large page sizes. GitHub-hosted
    runners include curl, whose TLS fingerprint is accepted by the public
    endpoint, so use POST requests with the site's supported 10-row paging.
    """
    out: dict[str, float] = {}
    endpoint = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
    for chunk_start, chunk_end in date_chunks(start, end):
        page_num = 1
        page_total = 1
        while page_num <= page_total:
            params = {
                "startDate": chunk_start.isoformat(),
                "endDate": chunk_end.isoformat(),
                "currency": "USD/CNY",
                "pageNum": page_num,
                "pageSize": 10,
            }
            url = f"{endpoint}?{urlencode(params)}"
            try:
                result = subprocess.run(
                    [
                        "curl", "--fail", "--silent", "--show-error",
                        "--max-time", "40", "--retry", "3",
                        "--retry-all-errors", "-X", "POST", url,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                payload = json.loads(result.stdout)
            except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                raise RuntimeError(
                    f"ChinaMoney failed {chunk_start}..{chunk_end} page {page_num}: {detail}"
                ) from exc
            if payload.get("head", {}).get("rep_code") != "200":
                raise RuntimeError(payload.get("head", {}).get("rep_message") or "bad rep_code")
            page_total = int(payload.get("data", {}).get("pageTotal") or 1)
            for rec in payload.get("records", []):
                vals = rec.get("values") or []
                if vals and vals[0] not in (None, ""):
                    out[rec["date"]] = float(str(vals[0]).replace(",", ""))
            page_num += 1
    return out


def load_existing() -> dict[str, Row]:
    if not DATA_FILE.exists():
        return {}
    rows: dict[str, Row] = {}
    with DATA_FILE.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r["date"]] = Row(
                date=r["date"],
                reuters_estimate=float(r["reuters_estimate"]) if r.get("reuters_estimate") else None,
                official_fix=float(r["official_fix"]) if r.get("official_fix") else None,
                deviation_points=int(r["deviation_points"]) if r.get("deviation_points") else None,
                forecast_url=r.get("forecast_url", ""),
                official_url=r.get("official_url", "https://www.chinamoney.com.cn/chinese/bkccpr/"),
                actual_source=r.get("actual_source", ""),
                quality_note=r.get("quality_note", ""),
                retrieved_at=r.get("retrieved_at", ""),
            )
    return rows


def merge_rows(existing: dict[str, Row], estimates: dict[str, dict], official: dict[str, float]) -> dict[str, Row]:
    now = datetime.now(TZ).isoformat(timespec="seconds")
    dates = set(existing) | set(estimates) | set(official)
    out: dict[str, Row] = {}
    for d in sorted(dates):
        old = existing.get(d, Row(date=d))
        est_rec = estimates.get(d, {})
        estimate = est_rec.get("reuters_estimate", old.reuters_estimate)
        official_fix = official.get(d, old.official_fix)
        source = "chinamoney" if d in official else old.actual_source
        note = old.quality_note
        if official_fix is None and est_rec.get("investinglive_actual") is not None:
            official_fix = est_rec["investinglive_actual"]
            source = "investinglive_fallback"
            note = "official_api_missing; using published actual article"
        if estimate is None and est_rec.get("actual_article_estimate") is not None:
            # Fallback only. Dedicated forecast article takes precedence because actual titles can contain typos.
            estimate = est_rec["actual_article_estimate"]
            note = "forecast_article_missing; estimate taken from actual article title"
        deviation = None
        if estimate is not None and official_fix is not None:
            deviation = int(round((official_fix - estimate) * 10000))
        out[d] = Row(
            date=d,
            reuters_estimate=estimate,
            official_fix=official_fix,
            deviation_points=deviation,
            forecast_url=est_rec.get("forecast_url", old.forecast_url),
            official_url="https://www.chinamoney.com.cn/chinese/bkccpr/",
            actual_source=source,
            quality_note=note,
            retrieved_at=now if (d in estimates or d in official) else old.retrieved_at,
        )
    return out


def save_csv(rows: dict[str, Row]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(Row(date="")).keys())
    with DATA_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in sorted(rows):
            r = asdict(rows[d])
            for key in ("reuters_estimate", "official_fix"):
                if r[key] is not None:
                    r[key] = f"{r[key]:.4f}"
            w.writerow(r)


def js_json(rows: list[Row]) -> str:
    payload = [asdict(r) for r in rows if r.reuters_estimate is not None or r.official_fix is not None]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def render_dashboard(rows: dict[str, Row], start: date) -> None:
    complete = [rows[d] for d in sorted(rows) if rows[d].reuters_estimate is not None and rows[d].official_fix is not None]
    data_json = js_json(complete)
    JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    JSON_FILE.write_text(json.dumps([asdict(r) for r in complete], ensure_ascii=False, indent=2), encoding="utf-8")
    latest = complete[-1] if complete else Row(date="--")
    coverage_ok = bool(complete and complete[0].date <= (start + timedelta(days=10)).isoformat() and len(complete) > 400)
    status = "历史回填完成" if coverage_ok else "当前为种子数据；首次联网运行后自动回填2024年至今"
    status_class = "ok" if coverage_ok else "warn"
    template = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>人民币中间价偏差监测</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--text:#17202a;--muted:#667085;--border:#e4e7ec;--accent:#175cd3;--pos:#b42318;--neg:#067647}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 50px}.head{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:18px}.head h1{font-size:27px;margin:0 0 7px}.sub{color:var(--muted);font-size:14px}.badge{padding:7px 10px;border-radius:999px;font-size:12px;font-weight:650;white-space:nowrap}.badge.ok{background:#ecfdf3;color:#067647}.badge.warn{background:#fffaeb;color:#b54708}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0}.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:17px 18px;box-shadow:0 1px 2px rgba(16,24,40,.04)}.label{color:var(--muted);font-size:13px;margin-bottom:10px}.value{font-size:27px;font-weight:720;letter-spacing:.2px}.value.small{font-size:20px}.unit{font-size:13px;color:var(--muted);margin-left:4px}.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;margin-top:14px;padding:18px;box-shadow:0 1px 2px rgba(16,24,40,.04)}.panel-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.panel-title h2{font-size:17px;margin:0}.switch button{border:1px solid var(--border);background:#fff;padding:6px 10px;cursor:pointer;font-size:12px}.switch button:first-child{border-radius:8px 0 0 8px}.switch button:last-child{border-radius:0 8px 8px 0}.switch button.active{background:#eff4ff;color:var(--accent);border-color:#b2ccff}.chart-wrap{width:100%;height:330px}.axis-label{font-size:11px;fill:#667085}.grid{stroke:#eaecf0;stroke-width:1}.zero{stroke:#98a2b3;stroke-width:1.2;stroke-dasharray:5 4}.line{fill:none;stroke:#175cd3;stroke-width:2}.dot{fill:#175cd3}.tooltip{position:fixed;display:none;background:#101828;color:#fff;padding:7px 9px;border-radius:7px;font-size:12px;pointer-events:none;z-index:5}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:11px 10px;border-bottom:1px solid var(--border);text-align:right}th{font-size:12px;color:var(--muted);font-weight:650;background:#f9fafb}th:first-child,td:first-child{text-align:left}.positive{color:var(--pos);font-weight:650}.negative{color:var(--neg);font-weight:650}.foot{margin-top:12px;color:var(--muted);font-size:12px;line-height:1.7}.foot a{color:var(--accent);text-decoration:none}@media(max-width:780px){.cards{grid-template-columns:repeat(2,1fr)}.head{align-items:flex-start;flex-direction:column}.chart-wrap{height:280px}.panel{padding:14px}th,td{padding:10px 6px}.hide-mobile{display:none}}@media(max-width:460px){.cards{grid-template-columns:1fr 1fr}.value{font-size:22px}}
</style></head><body><div class="wrap">
<div class="head"><div><h1>人民币中间价偏差监测</h1><div class="sub">Reuters estimate 与中国外汇交易中心官方 USD/CNY 中间价</div></div><div class="badge STATUS_CLASS">STATUS_TEXT</div></div>
<div class="cards"><div class="card"><div class="label">最新交易日</div><div class="value small" id="latestDate">LATEST_DATE</div></div><div class="card"><div class="label">Reuters预测</div><div class="value" id="latestEstimate">LATEST_EST</div></div><div class="card"><div class="label">官方实际</div><div class="value" id="latestFix">LATEST_FIX</div></div><div class="card"><div class="label">偏差（实际－预测）</div><div class="value" id="latestDev">LATEST_DEV<span class="unit">点</span></div></div></div>
<div class="panel"><div class="panel-title"><h2>偏差走势图</h2><div class="switch"><button data-range="30" class="active">30日</button><button data-range="90">90日</button><button data-range="all">全部</button></div></div><div class="chart-wrap" id="chart"></div></div>
<div class="panel"><div class="panel-title"><h2>最近7个交易日</h2></div><div style="overflow:auto"><table><thead><tr><th>日期</th><th>Reuters预测</th><th>官方实际</th><th>偏差（点）</th><th class="hide-mobile">方向</th></tr></thead><tbody id="recent"></tbody></table></div><div class="foot">偏差 =（官方实际中间价－Reuters预测值）× 10,000。正值表示官方 USD/CNY 中间价更高，即人民币定盘弱于市场化预测；负值相反。<br>数据源：<a href="https://investinglive.com/Tag/cny/" target="_blank">investingLive / Reuters estimate</a>；<a href="https://www.chinamoney.com.cn/chinese/bkccpr/" target="_blank">中国货币网</a>。定时任务：工作日北京时间09:20。</div></div>
</div><div class="tooltip" id="tip"></div><script>
const rows=DATA_JSON;
const fmt=x=>x==null?'--':Number(x).toFixed(4);
const recent=document.getElementById('recent');
rows.slice(-7).reverse().forEach(r=>{const tr=document.createElement('tr');const cls=r.deviation_points>0?'positive':r.deviation_points<0?'negative':'';const dir=r.deviation_points>0?'人民币定盘偏弱':r.deviation_points<0?'人民币定盘偏强':'持平';tr.innerHTML=`<td>${r.date}</td><td>${fmt(r.reuters_estimate)}</td><td>${fmt(r.official_fix)}</td><td class="${cls}">${r.deviation_points>0?'+':''}${r.deviation_points??'--'}</td><td class="hide-mobile ${cls}">${dir}</td>`;recent.appendChild(tr)});
const chart=document.getElementById('chart'),tip=document.getElementById('tip');
function draw(range){let data=rows.filter(x=>x.deviation_points!=null);if(range!=='all')data=data.slice(-Number(range));chart.innerHTML='';if(!data.length){chart.textContent='暂无数据';return}const W=Math.max(chart.clientWidth,320),H=chart.clientHeight,P={l:52,r:18,t:18,b:42};const vals=data.map(x=>x.deviation_points),min=Math.min(...vals,0),max=Math.max(...vals,0),pad=Math.max((max-min)*.12,20),lo=min-pad,hi=max+pad;const x=i=>P.l+(W-P.l-P.r)*(data.length===1?.5:i/(data.length-1));const y=v=>P.t+(H-P.t-P.b)*(hi-v)/(hi-lo);const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.setAttribute('width','100%');svg.setAttribute('height','100%');for(let i=0;i<5;i++){const v=lo+(hi-lo)*i/4,yy=y(v),line=document.createElementNS(ns,'line');line.setAttribute('x1',P.l);line.setAttribute('x2',W-P.r);line.setAttribute('y1',yy);line.setAttribute('y2',yy);line.setAttribute('class','grid');svg.appendChild(line);const t=document.createElementNS(ns,'text');t.setAttribute('x',P.l-8);t.setAttribute('y',yy+4);t.setAttribute('text-anchor','end');t.setAttribute('class','axis-label');t.textContent=Math.round(v);svg.appendChild(t)}const z=document.createElementNS(ns,'line');z.setAttribute('x1',P.l);z.setAttribute('x2',W-P.r);z.setAttribute('y1',y(0));z.setAttribute('y2',y(0));z.setAttribute('class','zero');svg.appendChild(z);const path=document.createElementNS(ns,'path');path.setAttribute('d',data.map((d,i)=>(i?'L':'M')+x(i)+' '+y(d.deviation_points)).join(' '));path.setAttribute('class','line');svg.appendChild(path);const step=Math.max(1,Math.ceil(data.length/7));data.forEach((d,i)=>{if(i%step===0||i===data.length-1){const t=document.createElementNS(ns,'text');t.setAttribute('x',x(i));t.setAttribute('y',H-14);t.setAttribute('text-anchor','middle');t.setAttribute('class','axis-label');t.textContent=d.date.slice(5);svg.appendChild(t)}const c=document.createElementNS(ns,'circle');c.setAttribute('cx',x(i));c.setAttribute('cy',y(d.deviation_points));c.setAttribute('r',data.length>100?2.2:3.4);c.setAttribute('class','dot');c.addEventListener('mousemove',e=>{tip.style.display='block';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY-35)+'px';tip.textContent=`${d.date}  ${d.deviation_points>0?'+':''}${d.deviation_points}点`});c.addEventListener('mouseleave',()=>tip.style.display='none');svg.appendChild(c)});chart.appendChild(svg)}
document.querySelectorAll('.switch button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.switch button').forEach(x=>x.classList.remove('active'));b.classList.add('active');draw(b.dataset.range)});draw('30');window.addEventListener('resize',()=>draw(document.querySelector('.switch button.active').dataset.range));
</script></body></html>'''
    replacements = {
        "STATUS_CLASS": status_class,
        "STATUS_TEXT": html.escape(status),
        "LATEST_DATE": latest.date,
        "LATEST_EST": "--" if latest.reuters_estimate is None else f"{latest.reuters_estimate:.4f}",
        "LATEST_FIX": "--" if latest.official_fix is None else f"{latest.official_fix:.4f}",
        "LATEST_DEV": "--" if latest.deviation_points is None else f"{latest.deviation_points:+d}",
        "DATA_JSON": data_json,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    DASHBOARD_FILE.write_text(template, encoding="utf-8")


def today_beijing() -> date:
    return datetime.now(TZ).date()


def update(start: date, full: bool, wait_today: bool) -> None:
    existing = load_existing()
    if len(existing) < 300:
        full = True
    logging.info("mode=%s, existing rows=%s", "full" if full else "incremental", len(existing))
    estimate_data = crawl_investinglive(start, full=full)
    official = fetch_chinamoney(start, today_beijing())
    rows = merge_rows(existing, estimate_data, official)
    if wait_today and datetime.now(TZ).weekday() < 5:
        target = today_beijing().isoformat()
        for attempt in range(4):
            r = rows.get(target)
            if r and r.reuters_estimate is not None and r.official_fix is not None:
                break
            logging.info("today incomplete; retry %s/4", attempt + 1)
            time.sleep(120)
            estimate_data.update(crawl_investinglive(today_beijing() - timedelta(days=5), full=False))
            official.update(fetch_chinamoney(today_beijing() - timedelta(days=5), today_beijing()))
            rows = merge_rows(rows, estimate_data, official)
    save_csv(rows)
    render_dashboard(rows, start)
    complete = [r for r in rows.values() if r.reuters_estimate is not None and r.official_fix is not None]
    logging.info("saved %s rows (%s complete)", len(rows), len(complete))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--full", action="store_true")
    p.add_argument("--wait-today", action="store_true")
    p.add_argument("--render-only", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start = date.fromisoformat(args.start)
    if args.render_only:
        render_dashboard(load_existing(), start)
        return 0
    update(start, args.full, args.wait_today)
    return 0

if __name__ == "__main__":
    sys.exit(main())
