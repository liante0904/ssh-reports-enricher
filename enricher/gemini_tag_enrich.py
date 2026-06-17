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

BATCH_PROMPT = """Extract tags from these Korean financial report titles.
Return ONLY a JSON object with format: {"results": [{"id": N, "tags": [...], "sector": "...", "stock_names": [...], "report_type": "COMPANY|INDUSTRY|MACRO|STRATEGY|QUANT"}, ...]}
No markdown, no explanation.

Titles:
{titles}"""

BATCH_SIZE = 50  # 한 번에 처리할 제목 수


def extract_tags_batch(rows):
    """Gemini API 배치 호출 → 여러 제목 동시 태그 추출"""
    if not API_KEY or not rows:
        return {}
    try:
        genai.configure(api_key=API_KEY)
        model = genai.GenerativeModel(MODEL)

        titles_block = "\n".join(f"{rid}: {title}" for rid, title in rows)
        resp = model.generate_content(BATCH_PROMPT.format(titles=titles_block))
        text = resp.text.strip()
        if text.startswith("```"):
            text = re.sub(r"```\w*\n?", "", text).rstrip("```")

        data = json.loads(text)
        results = data.get("results", [])
        out = {}
        for r in results:
            rid = r.get("id")
            if rid:
                out[rid] = {
                    "tags": r.get("tags", []),
                    "sector": r.get("sector", "") or "",
                    "stock_names": r.get("stock_names", []),
                    "report_type": r.get("report_type", "") or "",
                }
        return out
    except Exception as e:
        print(f"  Gemini batch error: {e}", file=sys.stderr)
        return {}


def run(db_batch=500, max_batches=0):
    """DB에서 batch_size만큼 가져와서 LLM 배치로 처리"""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    batch_num = 0
    total = 0
    enriched = 0

    print(f"[gemini-tag] db_batch={db_batch} llm_batch={BATCH_SIZE} max={'∞' if max_batches==0 else max_batches}")

    while True:
        batch_num += 1
        cur.execute("""
            SELECT report_id, article_title
            FROM tbl_sec_reports
            WHERE (tags IS NULL OR tags = '[]')
              AND article_title IS NOT NULL AND article_title != ''
            ORDER BY report_id DESC
            LIMIT %s
        """, (db_batch,))
        rows = cur.fetchall()
        if not rows:
            print(f"[gemini-tag] no pending reports. done.")
            break

        total += len(rows)

        # BATCH_SIZE씩 나눠서 LLM 호출
        for i in range(0, len(rows), BATCH_SIZE):
            chunk = rows[i:i + BATCH_SIZE]
            results = extract_tags_batch(chunk)

            for rid, _ in chunk:
                r = results.get(rid)
                if not r:
                    continue
                cur.execute("""
                    UPDATE tbl_sec_reports
                    SET tags = %s, sector = %s, stock_names = %s, report_type = %s
                    WHERE report_id = %s
                """, (
                    json.dumps(r["tags"], ensure_ascii=False),
                    r["sector"],
                    json.dumps(r["stock_names"], ensure_ascii=False),
                    r["report_type"],
                    rid
                ))
                enriched += 1
            time.sleep(0.3)

        conn.commit()
        print(f"[gemini-tag] batch #{batch_num}: {enriched}/{total} enriched ({(enriched/total*100):.1f}%)")

        if max_batches > 0 and batch_num >= max_batches:
            break

    cur.close()
    conn.close()
    print(f"[gemini-tag] done: {enriched}/{total} enriched")


if __name__ == "__main__":
    run(
        batch_size=int(sys.argv[1]) if len(sys.argv) > 1 else 50,
        max_batches=int(sys.argv[2]) if len(sys.argv) > 2 else 0,
    )
