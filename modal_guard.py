"""
전역 모달 자동 닫기 가드 — JS 사이드 구현.

Playwright sync API는 단일 스레드 전용이라 백그라운드 Python 스레드에서 page 메서드를
호출할 수 없다(greenlet.error). 대신 페이지에 JS 스크립트를 주입해 브라우저 안에서
직접 MutationObserver + 500ms polling으로 모달을 자동 닫는다.

사용:
    import modal_guard
    modal_guard.install(page)   # 페이지 로드/이동마다 자동 적용
"""
import functools

print = functools.partial(print, flush=True)


# 페이지에 주입되는 JS — 모달 등장을 감지해 닫기 버튼 클릭
_GUARD_JS = r"""
(() => {
    if (window.__modal_guard_installed__) return;
    window.__modal_guard_installed__ = true;

    const CLOSE_SELECTORS = [
        "button[aria-label='close']",
        "button[aria-label='닫기']",
        "[role='dialog'] button[class*='close']",
        "[role='dialog'] button.btn-close",
        ".modal button[class*='close']",
        ".seller-layer-modal button[class*='close']",
        "[role='dialog'] button:has-text('닫기')",
    ];

    // 답글 모달은 textarea가 있으니 닫지 않는다.
    function isProtected(dlg) {
        return !!dlg.querySelector("textarea");
    }

    function tryClose() {
        const dialogs = document.querySelectorAll(
            "[role='dialog'], .modal.in, .seller-layer-modal.in, .uib-modal-window"
        );
        for (const dlg of dialogs) {
            if (isProtected(dlg)) continue;
            // 텍스트 기반 후보도 시도 — :has-text가 안 되는 querySelectorAll 대안
            const buttons = dlg.querySelectorAll("button");
            let closed = false;
            for (const btn of buttons) {
                const txt = (btn.textContent || "").trim();
                if (txt === "닫기" || txt === "확인" || txt === "나중에" || txt === "다음에") {
                    btn.click();
                    closed = true;
                    break;
                }
                const cls = (btn.className || "").toLowerCase();
                const aria = (btn.getAttribute("aria-label") || "").toLowerCase();
                if (cls.includes("close") || aria.includes("close") || aria === "닫기") {
                    btn.click();
                    closed = true;
                    break;
                }
            }
            if (closed) console.log("[modal_guard.js] 모달 닫음");
        }
    }

    // 주기 폴링 (느리게 뜨는 모달 대응)
    setInterval(tryClose, 600);

    // MutationObserver로 새 모달 즉시 감지
    const obs = new MutationObserver(() => {
        // microtask 안에서 너무 빨리 닫으면 React 렌더 충돌 가능 — setTimeout으로 살짝 미룬다
        setTimeout(tryClose, 100);
    });
    obs.observe(document.body, { childList: true, subtree: true });

    console.log("[modal_guard.js] installed");
})();
"""


def install(context_or_page) -> None:
    """context 또는 page에 모달 자동 닫기 JS를 주입.

    context에 주입하면 그 context의 모든 새 페이지/popup에 자동 적용된다.
    """
    try:
        context_or_page.add_init_script(_GUARD_JS)
        print("[modal_guard] JS 주입 완료")
    except Exception as e:
        print(f"[modal_guard] 주입 실패: {e}")


def apply_now(page) -> None:
    """이미 열린 페이지에 즉시 적용 (init_script는 다음 navigation부터 적용되므로 보조)."""
    try:
        page.evaluate(_GUARD_JS)
    except Exception as e:
        print(f"[modal_guard] apply_now 실패: {e}")
