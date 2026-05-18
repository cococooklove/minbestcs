"""
서비스 전역 모달/팝업 안전 처리 가드 (v2 — 보수적 정책).

핵심 원칙
---------
1. **자동으로 누르는 것은 "닫기/나중에/다음에/다시 보지 않기" 와 close 아이콘뿐.**
   → "확인/예/다운로드/등록/저장/삭제/결제/구매/동의" 는 절대 자동 클릭하지 않음.
   흐름 코드가 명시적으로 클릭해야 하는 신호로 본다.
2. **보호 대상 모달은 건드리지 않음.**
   - textarea 포함 (답글 작성)
   - input[type=password] 포함 (재인증)
   - 본문에 "다운로드를 계속", "리뷰 다운로드", "QR", "captcha", "이중인증" 포함
   - 부모/자체 class에 qr / captcha / device 포함
3. **모르는 모달은 무시.**
   allow-list 접근이라 새 종류 모달이 나와도 가만히 둔다. 흐름이 멈추면
   _snapshot_page_state 같은 디버깅 로그로 가시화 → 정책에 키워드 추가.
4. **로그를 남긴다.**
   닫힌 모달은 window.__modal_guard_log 배열에 push. drain_log(page)로
   Python 쪽에서 가져와 progress 콜백으로 흘림.

사용
----
    import modal_guard
    modal_guard.install(context)              # 신규 페이지 자동 적용
    modal_guard.apply_now(page)               # 이미 열린 페이지 즉시 적용
    modal_guard.attach_dialog_autoaccept(ctx) # 네이티브 alert/confirm 자동 수락
    modal_guard.drain_log(page)               # 누적된 닫기 로그 회수 (list[str])
"""
import functools

print = functools.partial(print, flush=True)


_GUARD_JS = r"""
(() => {
    if (window.__modal_guard_installed__) return;
    window.__modal_guard_installed__ = true;
    window.__modal_guard_log = window.__modal_guard_log || [];

    const CLOSE_TEXTS = ["닫기", "나중에", "다음에", "다시 보지 않기"];
    const PROTECT_BODY_KEYWORDS = [
        "다운로드를 계속",
        "리뷰 다운로드",
        "QR",
        "captcha",
        "이중인증",
    ];
    const PROTECT_CLASS_KEYWORDS = ["qr", "captcha", "device"];
    const DIALOG_SEL = [
        "[role='dialog']",
        ".modal.in",
        ".seller-layer-modal.in",
        ".uib-modal-window",
        "[class*='Modal'][class*='open']",
        "[class*='Popup'][class*='open']",
    ].join(", ");

    function isProtected(dlg) {
        if (dlg.querySelector("textarea")) return "textarea";
        if (dlg.querySelector("input[type='password']")) return "password";
        const text = dlg.textContent || "";
        for (const kw of PROTECT_BODY_KEYWORDS) {
            if (text.includes(kw)) return "body:" + kw;
        }
        // 자체 class + 가까운 부모 class 검사
        let el = dlg;
        for (let i = 0; i < 4 && el; i++) {
            const cls = (el.className || "").toString().toLowerCase();
            for (const kw of PROTECT_CLASS_KEYWORDS) {
                if (cls.includes(kw)) return "class:" + kw;
            }
            el = el.parentElement;
        }
        return null;
    }

    function isVisible(el) {
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }

    function logClosed(text, how) {
        const entry = `[${new Date().toISOString()}] (${how}) "${(text || "").replace(/\s+/g, " ").trim().slice(0, 80)}"`;
        window.__modal_guard_log.push(entry);
        // 누적 방지 — 최근 100개만 유지
        if (window.__modal_guard_log.length > 100) {
            window.__modal_guard_log.splice(0, window.__modal_guard_log.length - 100);
        }
        console.log("[modal_guard.js]", entry);
    }

    function tryClose() {
        const dialogs = document.querySelectorAll(DIALOG_SEL);
        for (const dlg of dialogs) {
            if (!isVisible(dlg)) continue;
            const protectReason = isProtected(dlg);
            if (protectReason) continue;

            const fullText = dlg.textContent || "";

            // 1) close 아이콘 우선
            const closes = dlg.querySelectorAll(
                "button[aria-label='close'], button[aria-label='닫기'], " +
                "button[class*='close'], button[class*='Close'], " +
                "[role='button'][aria-label='close'], [role='button'][aria-label='닫기']"
            );
            if (closes.length) {
                try { closes[0].click(); logClosed(fullText, "icon"); continue; }
                catch (e) { /* fall through */ }
            }

            // 2) 텍스트 기반 (allow-list) — "확인" 등은 절대 누르지 않음
            let clicked = false;
            for (const btn of dlg.querySelectorAll("button, [role='button']")) {
                const t = (btn.textContent || "").trim();
                if (CLOSE_TEXTS.includes(t)) {
                    try { btn.click(); logClosed(fullText, "text:" + t); clicked = true; }
                    catch (e) { /* ignore */ }
                    break;
                }
            }
            if (clicked) continue;

            // 3) 알 수 없는 모달은 그대로 둠 (로그도 남기지 않음 — 가시화는 _snapshot_page_state가 담당)
        }
    }

    // 600ms 주기 폴링 (느리게 뜨는 모달 대응)
    setInterval(tryClose, 600);

    // MutationObserver — 새 모달 즉시 감지
    const obs = new MutationObserver(() => {
        // 너무 빨리 닫으면 React 렌더 충돌 가능 — 살짝 미룬다
        setTimeout(tryClose, 100);
    });
    obs.observe(document.body, { childList: true, subtree: true });

    console.log("[modal_guard.js] installed (conservative policy)");
})();
"""


def install(context_or_page) -> None:
    """context 또는 page에 모달 자동 닫기 JS를 주입.

    context에 주입하면 그 context의 모든 새 페이지/popup에 자동 적용된다.
    """
    try:
        context_or_page.add_init_script(_GUARD_JS)
        print("[modal_guard] init_script 주입 완료 (보수적 정책)")
    except Exception as e:
        print(f"[modal_guard] 주입 실패: {e}")


def apply_now(page) -> None:
    """이미 열린 페이지에 즉시 적용 (init_script는 다음 navigation부터 적용되므로 보조)."""
    try:
        page.evaluate(_GUARD_JS)
    except Exception as e:
        print(f"[modal_guard] apply_now 실패: {e}")


def attach_dialog_autoaccept(context_or_page) -> None:
    """네이티브 alert()/confirm() 자동 수락 핸들러 부착.

    context에 부착하면 그 context의 모든 페이지에 적용된다.
    page에 부착하면 해당 페이지만.
    """
    try:
        # context는 .on("page", ...)를 지원, page는 직접 .on("dialog", ...)
        if hasattr(context_or_page, "pages"):
            # BrowserContext
            def _attach(p):
                try:
                    p.on("dialog", lambda d: d.accept())
                except Exception:
                    pass
            for p in context_or_page.pages:
                _attach(p)
            context_or_page.on("page", _attach)
        else:
            context_or_page.on("dialog", lambda d: d.accept())
    except Exception as e:
        print(f"[modal_guard] dialog 핸들러 부착 실패: {e}")


def drain_log(page) -> list:
    """가드가 누적한 닫기 로그를 회수하고 비운다. (list[str] 반환)"""
    try:
        entries = page.evaluate("() => { const x = window.__modal_guard_log || []; window.__modal_guard_log = []; return x; }") or []
        return entries
    except Exception:
        return []
