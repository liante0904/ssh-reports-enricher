#!/usr/bin/env python3
"""증권사 view page에서 본문 텍스트 추출 → article_text 컬럼에 저장"""
import json, re, sys, time, urllib.request
import psycopg2
from dotenv import load_dotenv
load_dotenv(override=False)

DB_CONFIG = {
    "host": "127.0.0.1", "port": 5432, "dbname": "ssh_reports_hub",
    "user": "ssh_reports_hub", "password": "dlrtmrja!",
}

EXTRACTORS = {
    "DS투자증권": {
        "url_like": "%board.php%",
        "parse": lambda html: _parse_ds(html),
    },
    "신한증권": {
        "url_like": "%view.do%",
        "parse": lambda html: _parse_shinhan(html),
    },
}

def _parse_ds(html):
    m = re.search(r'id="bo_v_con">(.*?)</div>\s*(?:</section|<script|<div class="bo_v_)', html, re.DOTALL)
    if not m: m = re.search(r'id="bo_v_con">(.*?)(?:</section)', html, re.DOTALL)
    if not m: m = re.search(r'id="bo_v_con">(.*?)</div>', html, re.DOTALL)
    if not m: return None
    clean = re.sub(r'<[^>]+>', ' ', m.group(1))
    clean = re.sub(r'&nbsp;', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:10000] if len(clean) > 30 else None

def _parse_shinhan(html):
    divs = re.findall(r'<div[^>]*>(.*?)</div>', html, re.DOTALL)
    for d in divs:
        clean = re.sub(r'<[^>]+>', ' ', d)
        clean = re.sub(r'&nbsp;', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if len(clean) > 200:
            return clean[:10000]
    return None

def fetch_html(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read()
        for enc in ['utf-8', 'euc-kr', 'cp949']:
            try: return html.decode(enc)
            except: continue
    except: pass
    return None

def run(batch=100):
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    
    for firm, cfg in EXTRACTORS.items():
        cur.execute("""
            SELECT report_id, article_url FROM tbl_sec_reports
            WHERE firm_nm = %s AND article_url LIKE %s
              AND (article_text IS NULL OR article_text = '')
            ORDER BY save_time DESC LIMIT %s
        """, (firm, cfg["url_like"], batch))
        rows = cur.fetchall()
        if not rows: continue
        
        updated = 0
        for rid, url in rows:
            html = fetch_html(url)
            if not html: continue
            text = cfg["parse"](html)
            if text:
                cur.execute("UPDATE tbl_sec_reports SET article_text=%s WHERE report_id=%s", (text, rid))
                updated += 1
            time.sleep(0.2)
        
        conn.commit()
        print(f"[article_text] {firm}: {updated}/{len(rows)} enriched")

    cur.close(); conn.close()

def run_one(firm: str, batch=100):
    if firm == "DS": firms = ["DS투자증권"]
    elif firm == "신한": firms = ["신한증권"]
    else: firms = [f for f in EXTRACTORS]
    
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    for f in firms:
        cfg = EXTRACTORS[f]
        cur.execute("SELECT report_id, article_url FROM tbl_sec_reports WHERE firm_nm=%s AND article_url LIKE %s AND (article_text IS NULL OR article_text='') ORDER BY save_time DESC LIMIT %s", (f, cfg["url_like"], batch))
        rows = cur.fetchall()
        updated = 0
        for rid, url in rows:
            html = fetch_html(url)
            if not html: continue
            text = cfg["parse"](html)
            if text:
                cur.execute("UPDATE tbl_sec_reports SET article_text=%s WHERE report_id=%s", (text, rid))
                updated += 1
            time.sleep(0.15)
        conn.commit()
        print(f"[article_text] {f}: {updated}/{len(rows)}")
    cur.close(); conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_one(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 100)
    else:
        run(100)
