#!/usr/bin/env python3
import argparse, json, re, time, os
from datetime import datetime, timezone
import requests, feedparser, yaml
from bs4 import BeautifulSoup
from dateutil import parser as dtp

KEYS = ["agent","agents","agent os","agent engine","agent builder","mcp","guardrails","governance","observability","trace","pricing","ga","general availability","deprecation","vertex","bedrock","langgraph"]

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True)
    ap.add_argument("--out", default="agent-radar.json")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--days", type=int, default=30)  # 时间窗口
    ap.add_argument("--notify", default="")          # Slack Webhook 可选
    return ap.parse_args()

def norm_date(s):
    if not s: return None
    try:
        dt = dtp.parse(s)
        if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def in_window(dt, days):
    if not dt: return False
    return (datetime.now(timezone.utc) - dt).days <= days

def fetch_feed(url):
    try:
        if url.endswith(".json"):
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            return {"json": r.json()}
        return {"feed": feedparser.parse(url)}
    except Exception:
        return {}

def pick_date(entry):
    for k in ["published", "updated", "date", "created"]:
        v = entry.get(k) or entry.get(f"{k}_parsed")
        if isinstance(v, time.struct_time):
            return datetime(*v[:6], tzinfo=timezone.utc)
        if isinstance(v, str):
            d = norm_date(v)
            if d: return d
    return None

def html_summary(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # 取首段落/描述
        p = soup.find("meta", {"name":"description"}) or soup.find("p")
        return (p.get("content") if p and p.has_attr("content") else (p.get_text(strip=True) if p else "")).strip()[:240]
    except Exception:
        return ""

def match_keywords(text, kws):
    txt = text.lower()
    return any(k.lower() in txt for k in kws)

def main():
    args = parse_args()
    cfg = yaml.safe_load(open(args.sources, "r", encoding="utf-8"))
    items = []

    for v in cfg.get("vendors", []):
        vendor = v["name"]
        aud = v.get("audience","dev")
        base_impact = int(v.get("impact", 3))
        base_risk = int(v.get("risk", 3))
        kws = list(set(KEYS + v.get("keywords", [])))
        for feed_url in v.get("feeds", []):
            data = fetch_feed(feed_url)
            entries = []
            if "feed" in data:
                entries = data["feed"].entries or []
            elif "json" in data:
                j = data["json"]
                # 常见 JSON feed 结构简单兼容
                entries = j.get("items", j.get("value", {}).get("items", []))
            for e in entries:
                title = e.get("title") or e.get("heading") or ""
                link = e.get("link") or e.get("url") or ""
                if not link: continue
                dt = pick_date(e) or norm_date(e.get("date_published"))
                if not in_window(dt, args.days): continue
                # 文本用于关键词匹配
                desc = e.get("summary") or e.get("content","")
                text = f"{title} {desc} {link}"
                if not match_keywords(text, kws): continue

                # 类型粗分
                lower = text.lower()
                tp = "upgrade"
                if "ga" in lower or "general availability" in lower or "launch" in lower or "introduc" in lower:
                    tp = "launch"
                if "deprecat" in lower or "sunset" in lower:
                    tp = "deprecation"
                if "pricing" in lower or "$" in lower or "price" in lower:
                    tp = "pricing"

                # 简要描述
                summ = (e.get("summary") or e.get("content_text") or "")[:220]
                if not summ:
                    summ = html_summary(link)

                item = {
                    "id": f"{dt.strftime('%Y-%m-%d')}-{vendor.lower()}-{re.sub(r'[^a-z0-9]+','-', title.lower()).strip('-')[:40]}",
                    "date": dt.strftime("%Y-%m-%d"),
                    "name": title[:120],
                    "vendor": vendor,
                    "type": tp,
                    "summary": summ,
                    "sources": [{"title": f"{vendor} source", "url": link}],
                    "impact": base_impact,
                    "risk": base_risk,
                    "audience": aud,
                    "tags": []
                }
                items.append(item)

    # 去重（按 vendor+name）
    seen = set()
    dedup = []
    for it in sorted(items, key=lambda x: (x["date"], x["vendor"]), reverse=True):
        key = (it["vendor"], it["name"])
        if key in seen: continue
        seen.add(key)
        dedup.append(it)

    out = {"items": dedup[: args.top]}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # Slack（可选）
    if args.notify:
        try:
            text_lines = [f"*Agent Radar – Top {len(out['items'])}*"]
            for it in out["items"]:
                text_lines.append(f"• *{it['name']}* — {it['vendor']} ({it['date']}) <{it['sources'][0]['url']}|link>")
            payload = {"text": "\n".join(text_lines)}
            requests.post(args.notify, json=payload, timeout=10)
        except Exception:
            pass
            
if __name__ == "__main__":
    main()
