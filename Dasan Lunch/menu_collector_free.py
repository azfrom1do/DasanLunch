"""
인스타그램 식당 3곳의 오늘 메뉴 이미지를 받아서 정리하는 프로그램 (무료 버전)

흐름: 게시물 수집(instaloader) -> 이미지 다운로드 -> EasyOCR로 글자 읽기
      -> SQLite 저장 -> index.html 생성

규칙
  - 오늘 올라온 게시물이 있으면 그 게시물의 이미지를 사용합니다.
  - 오늘 게시물이 없으면(또는 못 찾으면) 가장 최근 게시물의 이미지를 사용합니다.
  - 글자가 잘 안 읽혀도 이미지는 항상 보여줍니다.
  - 인스타그램 접속에 실패하면 이전에 가져온 결과를 그대로 보여줍니다.
  - 실행 내용은 화면과 run_log.txt 에 함께 남습니다.

사전 준비
  pip install instaloader easyocr requests
  아래 ACCOUNTS 에 식당 인스타 아이디 입력
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
# 글자 인식을 건너뛰고 이미지만 보여줄 계정 아이디 (예: {"bap_saenggak"})
IMAGE_ONLY: set[str] = {"bap_saenggak"}
# ================

BASE = Path(__file__).resolve().parent
IMG_DIR = BASE.parent / "images"
DB_PATH = BASE / "menus_ocr.db"
OUT_HTML = BASE.parent / "index.html"
LOG_PATH = BASE / "run_log.txt"

# 줄 끝에 있는 가격 (예: 7,000 / 7000원)
PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원?\s*$")


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS menus (
            post_date TEXT,
            restaurant TEXT,
            shortcode TEXT,
            name TEXT,
            price TEXT,
            image TEXT
        );
        CREATE TABLE IF NOT EXISTS images (
            post_date TEXT,
            restaurant TEXT,
            shortcode TEXT,
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


def pick_targets(profile, today: dt.date):
    """오늘 올라온 게시물들. 없으면 가장 최근 게시물 1개. (게시물 목록, 대체 여부) 반환."""
    posts = []
    for i, post in enumerate(profile.get_posts()):
        if i >= MAX_POSTS_PER_ACCOUNT:
            break
        posts.append(post)
    if not posts:
        return [], False
    todays = [p for p in posts if p.date_local.date() == today]
    if todays:
        return todays, False
    return [max(posts, key=lambda p: p.date_utc)], True


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
            targets, is_fallback = pick_targets(profile, today)
        except Exception as e:
            print(f"  ! 게시물을 가져오지 못했습니다 (이전 결과를 그대로 사용): {e}")
            continue
        if not targets:
            print("  게시물이 없습니다")
            continue
        if is_fallback:
            print("  오늘 올라온 게시물이 없어 가장 최근 게시물을 사용합니다")

        # 이미 같은 게시물을 처리해 두었으면 건너뜀
        wanted = {p.shortcode for p in targets}
        saved = con.execute(
            "SELECT shortcode, image FROM images WHERE restaurant=?", (restaurant,)
        ).fetchall()
        if saved and {r[0] for r in saved} == wanted and all(
            (OUT_HTML.parent / r[1]).exists() for r in saved
        ):
            print("  이미 최신입니다")
            continue

        new_images, new_menus = [], []
        for post in targets:
            post_date = post.date_local.date().isoformat()
            print(f"  게시물 {post.shortcode} ({post_date})")
            try:
                urls = image_urls(post)
            except Exception as e:
                print(f"    ! 이미지 주소를 가져오지 못했습니다: {e}")
                continue
            for n, url in enumerate(urls):
                img = IMG_DIR / insta_id / f"{post.shortcode}_{n}.jpg"
                try:
                    if not img.exists():
                        download(url, img)
                except Exception as e:
                    print(f"    ! 이미지 {n} 다운로드 실패: {e}")
                    continue
                rel = img.relative_to(OUT_HTML.parent).as_posix()
                # 글자가 안 읽혀도 이미지는 항상 보여준다
                new_images.append((post_date, restaurant, post.shortcode, rel))
                if insta_id in IMAGE_ONLY:
                    continue
                try:
                    lines = ocr_lines(reader, img)
                except Exception as e:
                    print(f"    ! 글자 인식 실패 (이미지만 표시): {e}")
                    lines = []
                for line in lines:
                    name, price = split_price(line)
                    new_menus.append(
                        (post_date, restaurant, post.shortcode, name, price, rel)
                    )

        if not new_images:
            print("  ! 가져온 이미지가 없어 이전 결과를 그대로 사용합니다")
            continue
        con.execute("DELETE FROM images WHERE restaurant=?", (restaurant,))
        con.execute("DELETE FROM menus WHERE restaurant=?", (restaurant,))
        con.executemany("INSERT INTO images VALUES (?,?,?,?)", new_images)
        con.executemany("INSERT INTO menus VALUES (?,?,?,?,?,?)", new_menus)
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
        images = con.execute(
            "SELECT image, post_date FROM images WHERE restaurant=? ORDER BY rowid",
            (restaurant,),
        ).fetchall()
        rows = con.execute(
            "SELECT name, price FROM menus WHERE restaurant=? ORDER BY rowid",
            (restaurant,),
        ).fetchall()
        parts.append("<div class='col'>")
        parts.append(f"<h2>{html.escape(restaurant)}</h2>")
        if not images and not rows:
            parts.append("<p>아직 가져온 메뉴가 없습니다.</p></div>")
            continue
        post_date = images[0][1] if images else today
        if post_date != today:
            parts.append(
                f"<p class='note'>오늘 올라온 게시물이 없어 {html.escape(post_date)} "
                "게시물을 보여줍니다.</p>"
            )
        # 이미지 먼저
        for img, _ in images:
            parts.append(f"<img src='{html.escape(img)}' alt='메뉴 원본'>")
        # 그 다음 인식된 글자 (읽힌 글자가 있을 때만)
        if rows:
            parts.append("<ul>")
            for name, price in rows:
                p = f"<span class='p'>{html.escape(price)}</span>" if price else ""
                parts.append(f"<li>{html.escape(name)}{p}</li>")
            parts.append("</ul>")
        parts.append("</div>")
    parts.append("</div></body></html>")
    OUT_HTML.write_text("\n".join(parts), encoding="utf-8")
    print(f"완료: {OUT_HTML}")


def cleanup_images(con: sqlite3.Connection) -> None:
    """페이지에서 쓰지 않는 옛 이미지 파일을 지운다 (저장소가 커지는 것을 막음)."""
    used = {
        (OUT_HTML.parent / r[0]).resolve()
        for r in con.execute("SELECT image FROM images").fetchall()
    }
    removed = 0
    for f in IMG_DIR.rglob("*.jpg"):
        if f.resolve() not in used:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"안 쓰는 옛 이미지 {removed}개를 정리했습니다")


class Tee:
    """콘솔 화면과 로그 파일에 같은 내용을 동시에 출력."""

    def __init__(self, stream, log):
        self.stream = stream
        self.log = log

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:
            pass
        self.log.write(s)
        self.log.flush()
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        self.log.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


if __name__ == "__main__":
    log_file = open(LOG_PATH, "w", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] 시작")

    con = init_db()
    if "--render-only" not in sys.argv:
        collect(con)
        cleanup_images(con)
    render(con)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] 끝")
