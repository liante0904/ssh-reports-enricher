#!/usr/bin/env python3
"""
Gemini API 기반 태그 추출 배치
- enricher 키워드 사전보다 훨씬 높은 커버리지
- 제목만으로 tags, sector, stock_names, report_type 추출

사용법:
  python gemini_tag_enrich.py [batch_size] [max_batches]
"""

import json, os, re, sys, time
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv(override=False)

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "dbname": os.getenv("POSTGRES_REPORT_DB", "ssh_reports_hub"),
    "user": os.getenv("POSTGRES_USER", "ssh_reports_hub"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("ANTIGRAVITY_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
MODEL = "gemini-2.0-flash"

PROMPT = """Extract from this Korean financial report title. Return ONLY valid JSON, no markdown.

{
  "tags": ["keyword1", "keyword2"],
  "sector": "industry sector in Korean",
  "stock_names": ["stock name in Korean"],
  "report_type": "COMPANY|INDUSTRY|MACRO|STRATEGY|QUANT"
}

Title: {title}"""


def extract_tags(title):
    """Gemini API 호출 → 태그 추출"""
    if not API_KEY:
        return None
    try:
        genai.configure(api_key=API_KEY)
        model = genai.GenerativeModel(MODEL)
        resp = model.generate_content(PROMPT.format(title=title))
        text = resp.text.strip()
        # JSON 파싱
        if text.startswith("```"):
            text = re.sub(r"```\w*\n?", "", text).rstrip("```")
        return json.loads(text)
    except Exception as e:
        print(f"  Gemini error: {e}", file=sys.stderr)
        return None


def run(batch_size=50, max_batches=0):
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    batch_num = 0
    total = 0
    enriched = 0

    print(f"[gemini-tag] batch={batch_size} max_batches={'∞' if max_batches==0 else max_batches}")

    while True:
        batch_num += 1
        cur.execute("""
            SELECT report_id, article_title
            FROM tbl_sec_reports
            WHERE (tags IS NULL OR tags = '[]')
              AND article_title IS NOT NULL AND article_title != ''
            ORDER BY report_id DESC
            LIMIT %s
        """, (batch_size,))
        rows = cur.fetchall()
        if not rows:
            print(f"[gemini-tag] no pending reports. done.")
            break

        for rid, title in rows:
            total += 1
            result = extract_tags(title)
            if not result:
                continue

            tags_json = json.dumps(result.get("tags", []), ensure_ascii=False)
            sector = result.get("sector", "") or ""
            stock_names = json.dumps(result.get("stock_names", []), ensure_ascii=False)
            report_type = result.get("report_type", "") or ""

            cur.execute("""
                UPDATE tbl_sec_reports
                SET tags = %s, sector = %s, stock_names = %s, report_type = %s
                WHERE report_id = %s
            """, (tags_json, sector, stock_names, report_type, rid))
            enriched += 1

        conn.commit()
        print(f"[gemini-tag] batch #{batch_num}: {enriched}/{total} enriched")

        if max_batches > 0 and batch_num >= max_batches:
            break
        time.sleep(0.5)  # rate limit

    cur.close()
    conn.close()
    print(f"[gemini-tag] done: {enriched}/{total} enriched")


if __name__ == "__main__":
    run(
        batch_size=int(sys.argv[1]) if len(sys.argv) > 1 else 50,
        max_batches=int(sys.argv[2]) if len(sys.argv) > 2 else 0,
    )
