#!/usr/bin/env python3
import argparse, json, re, os, time
from datetime import datetime, timezone
import requests, feedparser, yaml
from bs4 import BeautifulSoup
from dateutil import parser as dtp

# 关键词兜底（与每个 vendor 的 keywords 合并去重）
GLOBAL_KEYS = [
  "agent","agents","agent engine","agent builder","agentspace","a2a","mcp",
  "governance","rbac","observability","pricing","ga","general availability",
  "deprecate","deprecation","sunset","identity","memory"
]

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True)
    ap.add_argument("--in", dest="infile", default="")
    ap.add_argument("--out", default="data/agent-radar.json")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--notify", default="")
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

def html_summary(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        m = soup.find("meta", {"name":"description"})
        if m and m.get("content"): return m["content"][:240]
        p = soup.find("p")
        return (p.get_text(strip=True) if p else "")[:240]
    except Exception:
        return ""

def pick_date(entry):
    for k in ["published","updated","date","created"]:
        v = entry.get(k) or entry.get(f"{k}_parsed")
        if isinstance(v, time.struct_time):
            return datetime(*v[:6], tzinfo=timezone.utc)
        if isinstance(v, str):
            d = norm_date(v)
            if d: return d
    # JSON feed常见字段
    if "date_published" in entry:
        return norm_date(entry.get("date_published"))
    return None

def fetch_feed(url):
    try:
        if url.endswith(".json"):
            return {"json": requests.get(url, timeout=20).json()}
        return {"feed": feedparser.parse(url)}
    except Exception:
        return {}

def match(text, kws):
    t = (text or "").lower()
    return any(k.lower() in t for k in kws)

def classify(text):
    t = (text or "").lower()
    if "deprecat" in t or "sunset" in t: return "deprecation"
    if "pricing" in t or "price" in t or "$" in t: return "pricing"
    if "ga" in t or "general availability" in t or "launch" in t or "introduc" in t: return "launch"
    return "upgrade"

def safe_id(date_str, vendor, title):
    slug = re.sub(r"[^a-z0-9]+","-", (title or "").lower()).strip("-")[:50]
    vendor_slug = re.sub(r"[^a-z0-9]+","-", (vendor or "").lower()).strip("-")
    return f"{date_str}-{vendor_slug}-{slug}"

def load_existing(path):
    if not path or not os.path.exists(path): return []
    try:
        j = json.load(open(path, "r", encoding="utf-8"))
        return j if isinstance(j, list) else j.get("items", [])
    except Exception:
        return []

def save_out(path, items):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)

def main():
    args = parse_args()
    cfg = yaml.safe_load(open(args.sources, "r", encoding="utf-8"))

    existing = load_existing(args.infile)  # list 或 {"items":[]}
    seen = set((it.get("id") or "") for it in existing if isinstance(it, dict))
    merged = [it for it in existing if isinstance(it, dict)]

    for v in cfg.get("vendors", []):
        vendor = v["name"]
        aud = v.get("audience","dev")
        base_impact = int(v.get("impact", 3))
        base_risk = int(v.get("risk", 3))
        kws = sorted(set(GLOBAL_KEYS + v.get("keywords", [])))
        for feed_url in v.get("feeds", []):
            data = fetch_feed(feed_url)
            entries = []
            if "feed" in data:
                entries = data["feed"].entries or []
            elif "json" in data:
                j = data["json"]
                entries = j.get("items", j.get("value", {}).get("items", [])) or []
            for e in entries:
                title = e.get("title") or e.get("heading") or ""
                link = e.get("link") or e.get("url") or ""
                if not link or not title: continue
                dt = pick_date(e)
                if not in_window(dt, args.days): continue
                text = f"{title} {e.get('summary') or e.get('content_text') or ''} {link}"
                if not match(text, kws): continue

                ttype = classify(text)
                date_str = dt.strftime("%Y-%m-%d")
                eid = safe_id(date_str, vendor, title)
                if eid in seen: continue

                summ = (e.get("summary") or e.get("content_text") or "")[:220]
                if not summ:
                    summ = html_summary(link)

                item = {
                    "id": eid,
                    "date": date_str,
                    "name": title[:160],
                    "vendor": vendor,
                    "type": ttype,
                    "summary": summ,
                    "sources": [{"title": f"{vendor} source", "url": link}],
                    "impact": base_impact,          # 展示默认值：若缺省则仪表盘显示时会回落到 0
                    "risk": base_risk,              # 过滤缺省视为 5；这里给出 vendor 缺省
                    "audience": aud,
                    "tags": []
                }
                merged.append(item)
                seen.add(eid)

    # 排序 & 截断
    def key_date(it): return it.get("date") or ""
    merged.sort(key=lambda x: key_date(x), reverse=True)
    merged = merged[: args.top]

    save_out(args.out, merged)

    # Slack（可选）
    if args.notify:
        try:
            lines = [f"*Agent Radar – Updated {len(merged)} items*"]
            for it in merged[:10]:
                lines.append(f"• {it['date']} — *{it['name']}* ({it['vendor']}) <{it['sources'][0]['url']}|source>")
            requests.post(args.notify, json={"text": "\n".join(lines)}, timeout=10)
        except Exception:
            pass

if __name__ == "__main__":
    main()
