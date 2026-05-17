"""
스마트스토어 셀러센터 자동화에 사용되는 셀렉터/URL을 한 곳에 모은다.
UI가 바뀌면 이 파일만 수정하면 된다.
"""

# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------
SELLER_HOME = "https://sell.smartstore.naver.com/"
REVIEW_SEARCH = "https://sell.smartstore.naver.com/#/review/search"

NAVER_LOGIN = "https://nid.naver.com/nidlogin.login"
LOGIN_URL_HINTS = ("login", "nidlogin", "oauth", "signin")
INTERVENTION_URL_HINTS = (
    "nidregisterdevice",  # 새 기기 등록
    "captcha",
    "deviceconfirm",
    "otp",
    "twofactor",
    "info/help",
)


# ---------------------------------------------------------------------------
# 네이버 로그인 페이지 (nid.naver.com)
# ---------------------------------------------------------------------------
LOGIN = {
    "id_input": "#id",
    "pw_input": "#pw",
    "keep_login_check": "#keep",
    "submit_btn": ".btn_login, button[type=submit], input[type=submit]",
    "captcha_indicators": "#captchaimg, .captcha_img, #captcha",
}


# ---------------------------------------------------------------------------
# 리뷰 검색·관리 페이지 (#/review/search)
# ---------------------------------------------------------------------------
REVIEW_PAGE = {
    # 검색 필터
    "reset_btn": "button:has-text('초기화')",
    "period_1year": "button:has-text('1년')",
    "period_6months": "button:has-text('6개월')",
    "period_3months": "button:has-text('3개월')",
    "period_1month": "button:has-text('1개월')",
    "search_btn": "button:has-text('검색')",
    # 검색어 입력
    "search_type_select": "select",                              # 첫 select가 검색유형
    "search_type_order_label": "상품주문번호",                   # select_option(label=...)
    "search_input_placeholder": "입력 후 검색하세요.",            # get_by_placeholder (2026-05 확인)
    "search_input_placeholder_legacy": "검색어를 입력해 주세요",  # 구버전 fallback
    # 결과/다운로드
    "excel_download_text": "엑셀다운",                           # get_by_text(...).first
    "visible_buttons": "button:visible",                         # 디버깅용
}


# ---------------------------------------------------------------------------
# 답글 작성 모달
# ---------------------------------------------------------------------------
REPLY_MODAL = {
    "open_btn_text": "답글작성",         # get_by_text(...).first
    "textarea": "textarea",              # .last (모달 가장 안쪽)
    "submit_candidates": [
        "button:has-text('등록')",
        "button:has-text('확인')",
        "button:has-text('저장')",
        "[class*='btn'][class*='primary']",
    ],
}


# ---------------------------------------------------------------------------
# 공통 팝업/모달 닫기 (광고·안내 팝업 자동 닫기에 사용)
# ---------------------------------------------------------------------------
COMMON_POPUP = {
    "confirm_candidates": [
        "[role='dialog'] button:has-text('확인')",
        "[role='dialog'] button:has-text('다운로드')",
        "[role='dialog'] button:has-text('예')",
        "[class*='Modal'] button:has-text('확인')",
        "[class*='modal'] button:has-text('확인')",
        "[class*='Popup'] button:has-text('확인')",
        "[class*='popup'] button:has-text('확인')",
        "[class*='Modal'] button[class*='primary']",
        "[class*='modal'] button[class*='confirm']",
    ],
}
