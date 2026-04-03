"""
네이버 스마트스토어 자동 로그인 및 세션 저장
실행: python3 login.py
"""
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import os, time

load_dotenv()

PROFILE_DIR = os.path.abspath("data/browser_profile")
NAVER_ID = os.environ.get("NAVER_ID", "")
NAVER_PW = os.environ.get("NAVER_PW", "")


def wait_for_seller_center(page):
    """셀러센터 진입 대기 (최대 5분)"""
    print("셀러센터 로그인 대기 중... (브라우저에서 로그인 완료 후 자동으로 닫힙니다)")
    for _ in range(300):
        url = page.url.lower()
        # 셀러센터 진입 확인
        if "sell.smartstore.naver.com" in url and not any(
            x in url for x in ("login", "nidlogin", "oauth", "signin")
        ):
            print(f"셀러센터 진입 확인: {page.url}")
            return True
        time.sleep(1)
    print("로그인 시간 초과")
    return False


def main():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            os.remove(lp)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            slow_mo=50,
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto("https://sell.smartstore.naver.com/", timeout=15000)
        except Exception:
            pass

        wait_for_seller_center(page)

        print("세션 저장 완료")
        context.close()


if __name__ == "__main__":
    main()
