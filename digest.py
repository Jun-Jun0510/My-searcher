#!/usr/bin/env python3
"""a16z Digest: 収集 → 構造化 → トレンド集計 → サイト生成 → 通知メール"""
import argparse, datetime as dt, html, json, os, re, smtplib, sqlite3, sys, urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic, feedparser, trafilatura, yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DB = ROOT / CFG.get("db_path", "data/digest.db")
MODEL = CFG.get("model", "claude-sonnet-5")
GROUP = {f["name"]: f.get("group", "a16z") for f in CFG["feeds"]}
NOW = dt.datetime.now(dt.timezone.utc)
_client = None


def tracks_text():
    return "\n".join(f"- {t['name']}（目的: {t['purpose']}）例: {', '.join(t['keywords'])}"
                     for t in CFG["interests"])


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # ANTHROPIC_API_KEY
    return _client


def since(days):
    return (NOW - dt.timedelta(days=days)).isoformat()


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS items(url TEXT PRIMARY KEY, source TEXT, title TEXT,
        published TEXT, content TEXT, fetched_at TEXT, analysis TEXT);
    CREATE TABLE IF NOT EXISTS keywords(url TEXT, keyword TEXT, seen_at TEXT);
    CREATE TABLE IF NOT EXISTS predictions(url TEXT, claim TEXT, horizon TEXT, made_at TEXT);
    CREATE TABLE IF NOT EXISTS issues(sent_at TEXT, subject TEXT, html TEXT);
    CREATE TABLE IF NOT EXISTS company_mentions(url TEXT, company TEXT, seen_at TEXT, grp TEXT);
    CREATE TABLE IF NOT EXISTS portfolio(name TEXT PRIMARY KEY, sector TEXT, first_seen TEXT, baseline INTEGER);
    """)
    return con


# ---------- 1. 収集 ----------
def text_of(entry):
    raw = entry.content[0].get("value", "") if entry.get("content") else ""
    raw = raw or entry.get("summary", "")
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)


def full_text(url):
    """フィードが要約だけのとき、記事ページから本文を取る。取れなければ空文字"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal digest reader)"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as ex:
        print(f"[fulltext skip] {url}: {ex}", file=sys.stderr)
        return ""
    return trafilatura.extract(raw) or ""


def collect(con):
    new = 0
    for src in CFG["feeds"]:
        feed = feedparser.parse(src["url"])
        if not feed.entries:
            print(f"[skip] {src['name']}: {feed.get('bozo_exception', 'no entries')}", file=sys.stderr)
            continue
        for e in feed.entries:
            url = e.get("link")
            if not url:
                continue
            p = e.get("published_parsed") or e.get("updated_parsed")
            pub = dt.datetime(*p[:6], tzinfo=dt.timezone.utc).isoformat() if p else NOW.isoformat()
            if con.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone():
                continue
            text = text_of(e)
            if len(text) < CFG.get("fulltext_below", 1000):
                text = max(text, full_text(url), key=len)
            cur = con.execute(
                "INSERT OR IGNORE INTO items(url,source,title,published,content,fetched_at) VALUES(?,?,?,?,?,?)",
                (url, src["name"], e.get("title", ""), pub, text[: CFG.get("max_chars", 12000)], NOW.isoformat()))
            new += cur.rowcount
    con.commit()
    return new


# ---------- 1b. 行動データ: ポートフォリオ ----------
def fetch_portfolio():
    pc = CFG.get("portfolio")
    if not pc:
        return None
    req = urllib.request.Request(pc["url"], headers={"User-Agent": "Mozilla/5.0 (personal digest reader)"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as ex:
        print(f"[portfolio skip] {ex}", file=sys.stderr)
        return None
    found = {}
    for el in BeautifulSoup(raw, "html.parser").select(pc["item_selector"]):
        n = el.select_one(pc["name_selector"]) if pc.get("name_selector") else el
        name = n.get_text(" ", strip=True) if n else ""
        if name:
            sec = el.select_one(pc["sector_selector"]) if pc.get("sector_selector") else None
            found[name] = sec.get_text(" ", strip=True) if sec else ""
    if not found:
        print("[portfolio] 企業が0件でした。セレクタ、またはJS描画かどうかを確認してください", file=sys.stderr)
        return None
    return found


def update_portfolio(con):
    found = fetch_portfolio()
    if not found:
        return
    baseline = con.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0
    known = {n for (n,) in con.execute("SELECT name FROM portfolio")}
    new = [n for n in found if n not in known]
    con.executemany("INSERT INTO portfolio VALUES(?,?,?,?)",
                    [(n, found[n], NOW.isoformat(), int(baseline)) for n in new])
    con.commit()
    print(f"[portfolio] {'ベースライン' if baseline else '新規'} {len(new)}社")


PORT_SYS = """For each company, return ONLY JSON:
{"companies": [{"name": "...", "what_ja": "何をしている会社か日本語1文。確信がなければ null", "interest_tags": ["matching interest track names"]}]}
Do not guess. If you do not know the company, set what_ja to null and use only the sector hint for tags."""


def new_portfolio(con):
    rows = con.execute("SELECT name,sector,first_seen FROM portfolio WHERE baseline=0 AND first_seen>=? "
                       "ORDER BY first_seen DESC", (since(7),)).fetchall()
    if not rows:
        return []
    try:
        res = ask_json(PORT_SYS, f"Interest tracks:\n{tracks_text()}\n\n"
                       + json.dumps([{"name": n, "sector": s} for n, s, _ in rows], ensure_ascii=False))
        info = {c["name"]: c for c in res.get("companies", [])}
    except Exception as ex:
        print(f"[portfolio enrich fail] {ex}", file=sys.stderr)
        info = {}
    return [{"name": n, "sector": s, "date": f[:10], "what_ja": info.get(n, {}).get("what_ja"),
             "interest_tags": info.get(n, {}).get("interest_tags", [])} for n, s, f in rows]


# ---------- 2. 構造化 ----------
def ask_json(system, user, max_tokens=2000):
    msg = client().messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                   messages=[{"role": "user", "content": user}])
    txt = "".join(b.text for b in msg.content if b.type == "text").strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
    return json.loads(txt)


EXTRACT_SYS = """You analyze content published by a16z (a VC firm with portfolio interests).
Return ONLY a JSON object, no prose:
{"summary_ja": "日本語で2-3文の要約",
 "points_ja": ["記事の要点を4-6個、各1文。数字・固有名詞・根拠を残す"],
 "keywords": ["canonical English tech/market terms, lowercase, max 8"],
 "companies": ["company names mentioned"],
 "portfolio_mentions": ["companies the text indicates a16z has invested in"],
 "claims": [{"text_ja": "主張を日本語1文で", "type": "prediction|observation|opinion", "horizon": "e.g. 2027, or null"}],
 "interest_tags": ["names of the given interest tracks that genuinely apply (exact track names)"],
 "signal": 1-5  // novelty and specificity for anticipating future technology or demand
}"""


CO_SUFFIX = re.compile(r"(^株式会社\s*|\s*株式会社$|[,\s]+(inc|corp|corporation|ltd|co|llc|plc|gmbh)\.?$)", re.I)


def norm_co(name):
    return CO_SUFFIX.sub("", name.strip()).strip()


def analyze(con, window_days):
    rows = con.execute("SELECT url,source,title,published,content FROM items "
                       "WHERE analysis IS NULL AND published >= ?", (since(window_days),)).fetchall()
    print(f"analyzing {len(rows)} items")
    for url, src, title, pub, content in rows:
        try:
            a = ask_json(EXTRACT_SYS, f"Interest tracks:\n{tracks_text()}\nSource: {src}\nTitle: {title}\n\n{content}")
        except Exception as ex:
            print(f"[analyze fail] {title}: {ex}", file=sys.stderr)
            continue
        con.execute("UPDATE items SET analysis=? WHERE url=?", (json.dumps(a, ensure_ascii=False), url))
        for k in {k.strip().lower() for k in a.get("keywords", []) if k and k.strip()}:
            con.execute("INSERT INTO keywords VALUES(?,?,?)", (url, k, pub))
        for co in {norm_co(c) for c in a.get("companies", []) if c and c.strip()}:
            con.execute("INSERT INTO company_mentions VALUES(?,?,?,?)", (url, co, pub, GROUP.get(src, "a16z")))
        for c in a.get("claims", []):
            if c.get("type") == "prediction":
                con.execute("INSERT INTO predictions VALUES(?,?,?,?)", (url, c.get("text_ja", ""), c.get("horizon"), pub))
        con.commit()


# ---------- 3. トレンド集計 ----------
def trends(con):
    wk, base = since(7), since(35)
    cur = dict(con.execute("SELECT keyword, COUNT(DISTINCT url) FROM keywords WHERE seen_at>=? GROUP BY keyword", (wk,)))
    prev = dict(con.execute("SELECT keyword, COUNT(DISTINCT url) FROM keywords WHERE seen_at>=? AND seen_at<? "
                            "GROUP BY keyword", (base, wk)))
    first = dict(con.execute("SELECT keyword, MIN(seen_at) FROM keywords GROUP BY keyword"))
    new = sorted((k for k in cur if first[k] >= wk), key=lambda k: -cur[k])
    rising = [(k, c, prev.get(k, 0) / 4) for k, c in cur.items()
              if k not in new and c >= 2 and c > 2 * prev.get(k, 0) / 4]
    rising.sort(key=lambda x: -(x[1] - x[2]))
    return {"new": new[:15],
            "rising": [{"keyword": k, "this_week": c, "avg_prev4w": round(p, 2)} for k, c, p in rising[:10]]}


def company_trends(con):
    """記事中の言及企業を週ごとに集計し、動きのある企業（初登場・急上昇・複数ソース）を抽出する"""
    wk = since(7)
    first = dict(con.execute("SELECT LOWER(company), MIN(seen_at) FROM company_mentions GROUP BY LOWER(company)"))
    known = {n.lower() for (n,) in con.execute("SELECT name FROM portfolio")}
    C = {}
    for key, name, url, seen, grp in con.execute(
            "SELECT LOWER(company), company, url, seen_at, grp FROM company_mentions WHERE seen_at>=?", (since(56),)):
        c = C.setdefault(key, {"name": name, "weekly": [set() for _ in range(8)], "groups": set(), "urls": set()})
        age = (NOW - dt.datetime.fromisoformat(seen)).days
        c["weekly"][7 - min(7, max(0, age // 7))].add(url)
        if seen >= wk:
            c["groups"].add(grp)
            c["urls"].add(url)
    out = []
    for key, c in C.items():
        weekly = [len(x) for x in c["weekly"]]
        cur, avg = weekly[7], sum(weekly[3:7]) / 4
        if cur == 0:
            continue
        reasons = []
        if first[key] >= wk and cur >= 2:
            reasons.append("初登場")
        elif cur >= 2 and cur > 2 * avg:
            reasons.append("急上昇")
        if len(c["groups"]) >= 2:
            reasons.append("複数ソース")
        if key in known:
            reasons.append("a16z投資先")
        out.append({"name": c["name"], "this_week": cur, "avg_prev4w": round(avg, 2), "weekly": weekly,
                    "groups": sorted(c["groups"]), "reasons": reasons, "urls": sorted(c["urls"]),
                    "momentum": round(cur - avg + (1 if len(c["groups"]) >= 2 else 0), 2)})
    moving = {"初登場", "急上昇", "複数ソース"}
    focus = sorted((x for x in out if moving & set(x["reasons"])), key=lambda x: -x["momentum"])[:12]
    ranking = sorted(out, key=lambda x: (-x["this_week"], -x["momentum"]))[:20]
    return {"focus": focus, "ranking": ranking}


def old_predictions(con):
    rows = con.execute("SELECT claim,horizon,made_at FROM predictions WHERE made_at BETWEEN ? AND ? LIMIT 5",
                       (since(400), since(330))).fetchall()
    return [{"claim": c, "horizon": h, "made_at": m[:10]} for c, h, m in rows]


# ---------- 4. 編集 ----------
EDITOR_SYS = (
    "あなたは技術・投資ニュースレターの編集者です。読者像: " + CFG["reader_profile"] + "\n"
    "目的は、世の中の需要の把握と、今後伸びる技術・企業の先取りです。"
    "a16zはポートフォリオを持つVCなので、ポジショントークと思われる部分は冷静に指摘してください。"
    "入力は今週の記事分析（i=記事番号）、キーワード統計、約1年前の予測です。"
    "要約の羅列ではなく、複数記事を横断した考察を書いてください。"
    "group=scienceの記事は科学系メディア由来なので、a16zの主張とは区別し、長期の科学シグナルとして扱ってください。"
    "group=engineeringの記事は工学・業界メディア由来なので、a16zの主張の裏取り（実際に出荷・導入されているか）に使ってください。"
    "new_portfolioはa16zが今週新たに投資先として掲載した企業で、発言ではなく行動のデータです。記事の主張と整合しているかを見てください。"
    "interest_crossは記事があるトラックすべてについて書いてください。\n"
    "Return ONLY JSON:\n"
    '{"subject": "件名 25字以内", "tldr": ["今週の要点を3つ"],'
    ' "themes": [{"title": "...", "body": "3-5文の考察", "articles": [記事番号]}],'
    ' "new_signals": [{"keyword": "...", "why": "注目理由1文"}],'
    ' "interest_cross": [{"tag": "トラック名", "note": "そのトラックの目的に照らした意味1-2文"}],'
    ' "portfolio_note": "新規投資先から読み取れる動きと、記事の主張との整合 1-3文（新規投資先がなければ空文字）",'
    ' "company_note": "moving_companiesのうち注目すべき企業の動きと、その理由 2-3文（なければ空文字）",'
    ' "skeptic": "ポジショントーク・見落とし・反対意見の視点 2-4文",'
    ' "watch": ["今後ウォッチすべき問い 2-3個"]}'
)
KEEP = ("i", "source", "group", "title", "date", "summary_ja", "keywords", "claims",
        "interest_tags", "portfolio_mentions", "signal")


def build_issue(con):
    rows = con.execute("SELECT url,source,title,published,analysis FROM items "
                       "WHERE analysis IS NOT NULL AND published>=? ORDER BY published DESC", (since(7),)).fetchall()
    if not rows:
        return None
    arts = [{"i": i, "source": s, "group": GROUP.get(s, "a16z"), "title": t, "date": p[:10], **json.loads(a)}
            for i, (_, s, t, p, a) in enumerate(rows)]
    tr, olds, newp, ct = trends(con), old_predictions(con), new_portfolio(con), company_trends(con)
    payload = {"articles": [{k: v for k, v in x.items() if k in KEEP} for x in arts],
               "trends": tr, "old_predictions": olds, "new_portfolio": newp,
               "moving_companies": [{k: c[k] for k in ("name", "this_week", "avg_prev4w", "groups", "reasons")}
                                    for c in ct["focus"]],
               "interest_tracks": [{"name": t["name"], "purpose": t["purpose"]} for t in CFG["interests"]]}
    ed = ask_json(EDITOR_SYS, json.dumps(payload, ensure_ascii=False), max_tokens=4000)
    return ed, arts, [r[0] for r in rows], tr, olds, newp, ct


# ---------- 5. サイト生成 ----------
COLORS = ["#C98A00", "#0E8FA8", "#B8367A", "#3E8E41", "#3A63C4", "#C45A1E", "#7A4FC2", "#2A8C7A"]
ART_KEEP = ("i", "source", "group", "title", "date", "summary_ja", "points_ja", "claims", "companies",
            "portfolio_mentions", "interest_tags", "signal", "keywords")


def save_issue(con, ed, arts, urls, tr, newp, ct, period):
    d = ROOT / "data" / "issues"
    d.mkdir(parents=True, exist_ok=True)
    known = {n.lower() for (n,) in con.execute("SELECT name FROM portfolio")}
    out = []
    for a in arts:
        x = {**{k: a.get(k) for k in ART_KEEP}, "url": urls[a["i"]]}
        # 投資先の判定はLLMの推測より、ポートフォリオページの実データを優先して補う
        x["portfolio_mentions"] = sorted(set(a.get("portfolio_mentions") or [])
                                         | {c for c in a.get("companies") or [] if c.lower() in known})
        out.append(x)
    issue = {"date": str(NOW.date()), "period": period, "editorial": ed, "trends": tr,
             "new_portfolio": newp, "companies": ct, "articles": out}
    (d / f"{NOW.date()}.json").write_text(json.dumps(issue, ensure_ascii=False), encoding="utf-8")


def build_site(con):
    issues = [json.loads(p.read_text(encoding="utf-8"))
              for p in sorted((ROOT / "data" / "issues").glob("*.json"), reverse=True)]
    preds = [{"claim": c, "horizon": h, "date": m[:10], "url": u} for u, c, h, m in
             con.execute("SELECT url,claim,horizon,made_at FROM predictions ORDER BY made_at DESC LIMIT 300")]
    data = {"tracks": [{"name": t["name"], "purpose": t["purpose"], "color": COLORS[i % len(COLORS)]}
                       for i, t in enumerate(CFG["interests"])],
            "issues": issues, "predictions": preds}
    tpl = (ROOT / "site_template.html").read_text(encoding="utf-8")
    out = ROOT / "site" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")),
                   encoding="utf-8")
    return out


# ---------- 6. 通知メール（要点＋サイトへのリンク） ----------
def e(s):
    return html.escape(str(s if s is not None else ""))


def render_email(ed, period):
    url = CFG.get("site_url", "")
    items = "".join(f"<li style='margin:6px 0'>{e(x)}</li>" for x in ed.get("tldr", []))
    btn = (f'<p style="margin:24px 0"><a href="{e(url)}" style="background:#17222D;color:#fff;'
           f'padding:10px 18px;border-radius:6px;text-decoration:none">今週のダイジェストを開く</a></p>') if url else ""
    return ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#EDF0F2">'
            '<div style="max-width:600px;margin:0 auto;padding:24px;background:#fff;color:#17222D;'
            "font-family:-apple-system,'Hiragino Sans','Noto Sans JP',sans-serif;font-size:15px;line-height:1.7\">"
            f'<div style="font-size:13px;color:#5B6874">a16z Digest {e(period)}</div>'
            f'<h1 style="font-size:20px;margin:6px 0 12px">{e(ed.get("subject"))}</h1>'
            f'<ul style="padding-left:20px;margin:0">{items}</ul>{btn}</div></body></html>')


def send(subject, page):
    addr = os.environ["GMAIL_ADDRESS"]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, addr, os.environ.get("MAIL_TO") or addr
    msg.attach(MIMEText(BeautifulSoup(page, "html.parser").get_text("\n", strip=True), "plain", "utf-8"))
    msg.attach(MIMEText(page, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(addr, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true", help="収集だけ行う（日次用）")
    ap.add_argument("--dry-run", action="store_true", help="メールを送らずサイトだけ生成")
    ap.add_argument("--backfill-days", type=int, default=8, help="未分析記事を遡る日数（初回は35推奨）")
    a = ap.parse_args()

    con = db()
    print(f"collected {collect(con)} new items")
    if a.collect_only:
        return
    analyze(con, a.backfill_days)
    update_portfolio(con)
    res = build_issue(con)
    if not res:
        print("no items this week")
        return
    ed, arts, urls, tr, _, newp, ct = res
    period = f"{(NOW - dt.timedelta(days=7)).date()} – {NOW.date()}"
    save_issue(con, ed, arts, urls, tr, newp, ct, period)
    print(f"site: {build_site(con)}")
    if a.dry_run:
        return
    subject = f"[a16z週報] {ed.get('subject', '今週のダイジェスト')}"
    send(subject, render_email(ed, period))
    print(f"sent: {subject}")


if __name__ == "__main__":
    main()
