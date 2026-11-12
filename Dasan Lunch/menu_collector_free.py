"""
인스타그램 식당 3곳의 오늘 메뉴 이미지를 받아서 정리하는 프로그램 (무료 버전)

흐름: 게시물 수집(instaloader) -> 이미지 다운로드 -> EasyOCR로 글자 읽기
      -> SQLite 저장 -> today_menu.html 생성

사전 준비
  pip install instaloader easyocr requests
  아래 ACCOUNTS 에 식당 인스타 아이디 3개 입력
  (API 키와 결제는 필요 없습니다. 첫 실행 때 한글 모델을 자동으로 내려받습니다.)
"""
import datetime as dt
import html
import re
import sqlite3
import sys
from pathlib import Path

import easyocr
import instaloader
import requests

# ===== 설정 =====
ACCOUNTS = {
    "봄봄 식당": "bombom_dasan",
    "더 푸드 플러스": "thefood_plus",
    "밥생각": "bap_saenggak",
}
MAX_POSTS_PER_ACCOUNT = 12   # 고정 게시물이 앞에 올 수 있어서 여유 있게 확인
MIN_CONF = 0.3               # 이 값보다 자신 없게 읽은 글자는 버림 (0~1)
MIN_LINES = 3                # 읽은 줄이 이보다 적은 이미지는 메뉴판이 아니라고 보고 건너뜀
# ================

BASE = Path(__file__).resolve().parent
IMG_DIR = BASE / "images"
DB_PATH = BASE / "menus_ocr.db"
OUT_HTML = BASE / "today_menu.html"

# 줄 끝에 있는 가격 (예: 7,000 / 7000원)
PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원?\s*$")


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS posts (
            shortcode TEXT PRIMARY KEY,
            restaurant TEXT,
            post_date TEXT
        );
        CREATE TABLE IF NOT EXISTS menus (
            post_date TEXT,
            restaurant TEXT,
            shortcode TEXT,
            name TEXT,
            price TEXT,
            image TEXT
        );
        """
    )
    return con


def image_urls(post) -> list[str]:
    if post.typename == "GraphSidecar":
        return [n.display_url for n in post.get_sidecar_nodes()]
    return [post.url]


def download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    path.write_bytes(r.content)


def ocr_lines(reader: easyocr.Reader, path: Path) -> list[str]:
    """이미지를 읽어서 같은 높이의 글자 조각을 한 줄로 묶어 반환."""
    items = []
    # 경로에 한글이 있으면 OpenCV가 파일을 못 읽으므로, 바이트로 읽어서 넘긴다
    for box, text, conf in reader.readtext(path.read_bytes()):
        if conf < MIN_CONF or not text.strip():
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append({
            "y": (min(ys) + max(ys)) / 2,
            "x": min(xs),
            "h": max(ys) - min(ys),
            "text": text.strip(),
        })
    if not items:
        return []

    items.sort(key=lambda i: i["y"])
    med_h = sorted(i["h"] for i in items)[len(items) // 2]

    rows = [[items[0]]]
    for it in items[1:]:
        row_y = sum(i["y"] for i in rows[-1]) / len(rows[-1])
        if abs(it["y"] - row_y) <= med_h * 0.6:
            rows[-1].append(it)
        else:
            rows.append([it])

    return [" ".join(i["text"] for i in sorted(r, key=lambda i: i["x"])) for r in rows]


def split_price(line: str) -> tuple[str, str]:
    m = PRICE_RE.search(line)
    if not m:
        return line, ""
    return line[: m.start()].strip(), m.group(1)


def collect(con: sqlite3.Connection) -> None:
    print("OCR 모델 불러오는 중... (처음에는 다운로드 때문에 오래 걸립니다)")
    reader = easyocr.Reader(["ko", "en"], gpu=False)
    loader = instaloader.Instaloader(download_pictures=False, save_metadata=False,
                                     quiet=True)
    today = dt.date.today()

    for restaurant, insta_id in ACCOUNTS.items():
        print(f"[{restaurant}] @{insta_id}")
        try:
            profile = instaloader.Profile.from_username(loader.context, insta_id)
        except Exception as e:
            print(f"  ! 프로필을 가져오지 못했습니다: {e}")
            continue

        for i, post in enumerate(profile.get_posts()):
            if i >= MAX_POSTS_PER_ACCOUNT:
                break
            post_date = post.date_local.date()
            if post_date != today:
                continue
            if con.execute("SELECT 1 FROM posts WHERE shortcode=?",
                           (post.shortcode,)).fetchone():
                continue  # 이미 처리한 게시물

            print(f"  새 게시물 {post.shortcode}")
            for n, url in enumerate(image_urls(post)):
                img = IMG_DIR / insta_id / f"{post.shortcode}_{n}.jpg"
                download(url, img)
                lines = ocr_lines(reader, img)
                if len(lines) < MIN_LINES:
                    print(f"    이미지 {n}: 글자가 적어 건너뜀")
                    continue
                rel = img.relative_to(BASE).as_posix()
                for line in lines:
                    name, price = split_price(line)
                    con.execute(
                        "INSERT INTO menus VALUES (?,?,?,?,?,?)",
                        (post_date.isoformat(), restaurant, post.shortcode,
                         name, price, rel),
                    )
            con.execute("INSERT INTO posts VALUES (?,?,?)",
                        (post.shortcode, restaurant, post_date.isoformat()))
            con.commit()


def render(con: sqlite3.Connection) -> None:
    today = dt.date.today().isoformat()
    parts = [
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{today} 오늘의 메뉴</title>"
        "<style>"
        "body{font-family:sans-serif;max-width:1400px;margin:2rem auto;padding:0 1rem}"
        ".grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;align-items:start}"
        "@media(max-width:900px){.grid{grid-template-columns:1fr}}"
        ".col{min-width:0}"
        "h2{border-bottom:2px solid #888;padding-bottom:.3rem;margin-top:0}"
        "img{width:100%;display:block;margin-bottom:.6rem;border-radius:6px}"
        "ul{padding-left:1.2rem}"
        "li{margin:.2rem 0}.p{color:#888;margin-left:.5rem}"
        ".note{color:#888;font-size:.85rem}"
        "</style></head><body>",
        f"<h1>{today} 오늘의 메뉴</h1>",
        "<p class='note'>아래 글자는 인식(OCR) 결과라서 오타가 있을 수 있습니다. "
        "헷갈리면 위의 원본 이미지를 확인하세요.</p>",
        "<div class='grid'>",
    ]
    for restaurant in ACCOUNTS:
        rows = con.execute(
            "SELECT name, price, image FROM menus "
            "WHERE post_date=? AND restaurant=? ORDER BY rowid",
            (today, restaurant),
        ).fetchall()
        parts.append("<div class='col'>")
        parts.append(f"<h2>{html.escape(restaurant)}</h2>")
        if not rows:
            parts.append("<p>아직 올라온 메뉴가 없습니다.</p></div>")
            continue
        # 이미지 먼저
        images = list(dict.fromkeys(r[2] for r in rows))
        for img in images:
            parts.append(f"<img src='{html.escape(img)}' alt='메뉴 원본'>")
        # 그 다음 인식된 글자
        parts.append("<ul>")
        for name, price, _ in rows:
            p = f"<span class='p'>{html.escape(price)}</span>" if price else ""
            parts.append(f"<li>{html.escape(name)}{p}</li>")
        parts.append("</ul></div>")
    parts.append("</div></body></html>")
    OUT_HTML.write_text("\n".join(parts), encoding="utf-8")
    print(f"완료: {OUT_HTML}")


if __name__ == "__main__":
    con = init_db()
    if "--render-only" not in sys.argv:
        collect(con)
    render(con)
