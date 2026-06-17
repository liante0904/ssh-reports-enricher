#!/usr/bin/env python3
"""Gemini REST API 태그 추출 배치"""

import json, os, re, sys, time
import psycopg2, urllib.request
from dotenv import load_dotenv
load_dotenv(override=False)

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "dbname": os.getenv("POSTGRES_REPORT_DB", "ssh_reports_hub"),
    "user": os.getenv("POSTGRES_USER", "ssh_reports_hub"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}

API_KEY = os.getenv("GEMINI_API_KEY", "")
URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={API_KEY}"
LLM_BATCH = 10

PROMPT_TEMPLATE = """Return ONE JSON array for ALL titles below. Each: {{"id":N,"tags":[],"sector":"...","stock_names":[],"report_type":"COMPANY|INDUSTRY|MACRO|STRATEGY|QUANT"}}. Output ONLY the array, no markdown.

Titles:
{titles}"""


def call_gemini(rows):
    if not API_KEY or not rows: return {}
    try:
        titles_block = "\n".join(f"{rid}: {title}" for rid, title in rows)
        body = json.dumps({
            "contents": [{"parts": [{"text": PROMPT_TEMPLATE.format(titles=titles_block)}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192}
        }).encode()
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=45)
        data = json.loads(resp.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)
        text = text.replace("]\n[", ",\n").replace("][", ",").strip()

        parsed = json.loads(text)
        results = parsed if isinstance(parsed, list) else parsed.get("results", [])
        return {r["id"]: r for r in results if r.get("id")}
    except Exception as e:
        print(f"  Gemini error: {e}", file=sys.stderr)
        return {}


def run(db_batch=500, max_batches=0):
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    n, total, enriched = 0, 0, 0

    while True:
        n += 1
        cur.execute("""
            SELECT report_id, article_title FROM tbl_sec_reports
            WHERE (tags IS NULL OR tags = '[]')
              AND article_title IS NOT NULL AND article_title != ''
              AND reg_dt >= '20260101'
              AND (fnguide_summary_id IS NOT NULL OR report_type IS NOT NULL)
            ORDER BY fnguide_summary_id DESC NULLS LAST, report_id DESC
            LIMIT %s
        """, (db_batch,))
        rows = cur.fetchall()
        if not rows: break

        total += len(rows)
        for i in range(0, len(rows), LLM_BATCH):
            chunk = rows[i:i + LLM_BATCH]
            results = call_gemini(chunk)
            for rid, _ in chunk:
                r = results.get(rid)
                if not r: continue
                cur.execute("""
                    UPDATE tbl_sec_reports SET tags=%s, sector=%s, stock_names=%s, report_type=%s
                    WHERE report_id=%s
                """, (json.dumps(r.get("tags", []), ensure_ascii=False),
                      r.get("sector", "") or "",
                      json.dumps(r.get("stock_names", []), ensure_ascii=False),
                      r.get("report_type", "") or "", rid))
                enriched += 1
            time.sleep(0.3)

        conn.commit()
        print(f"[gemini] #{n}: {enriched}/{total} ({enriched/total*100:.1f}%)")
        if max_batches > 0 and n >= max_batches: break

    cur.close(); conn.close()
    print(f"[gemini] done: {enriched}/{total}")


if __name__ == "__main__":
    run(
        db_batch=int(sys.argv[1]) if len(sys.argv) > 1 else 500,
        max_batches=int(sys.argv[2]) if len(sys.argv) > 2 else 0,
    )
