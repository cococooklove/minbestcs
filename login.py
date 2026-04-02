"""
1단계: 로그인 후 세션 저장
실행: python3 login.py
"""
from playwright.sync_api import sync_playwright
import os, time

PROFILE_DIR = os.path.abspath("data/browser_profile")


def main():
    os.makedirs(PROFILE_DIR, exist_ok=True)

    # 이전 실행에서 남은 락 파일 정리
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lock_path = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lock_path):
            os.remove(lock_path)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            slow_mo=50,
            viewport={"width": 1440, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto("https://sell.smartstore.naver.com/", timeout=10000)
        except Exception:
            pass

        print("브라우저가 열렸습니다. 네이버 커머스 ID로 로그인해주세요.")
        print("스마트스토어 셀러센터 메인 화면이 뜰 때까지 기다립니다...\n")

        # 대시보드 또는 셀러센터 내부 페이지 진입 확인
        print("(로그인 완료를 자동 감지합니다...)")
        LOGIN_PATHS = ("login", "nidlogin", "naver.com/nid", "oauth")
        while True:
            url = page.url
            is_login_page = any(p in url.lower() for p in LOGIN_PATHS)
            if "sell.smartstore.naver.com" in url and not is_login_page:
                print(f"로그인 확인됨: {url}")
                time.sleep(2)
                break
            print(f"  대기 중: {url}")
            time.sleep(2)

        context.close()
        print(f"\n완료. 이제 python3 scraper.py 를 실행하세요.")


if __name__ == "__main__":
    main()
