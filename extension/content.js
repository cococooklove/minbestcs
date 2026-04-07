function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// MAIN world executeScript에서 보내는 진행 상황 수신
document.addEventListener('__minbest_progress', e => {
  sendProgress(e.detail);
});

async function clickByText(text, timeoutMs = 8000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const btn = [...document.querySelectorAll('button')].find(
      b => b.textContent.trim().includes(text) && b.offsetParent !== null && !b.disabled
    );
    if (btn) { btn.click(); return true; }
    await sleep(300);
  }
  throw new Error(`버튼 "${text}"를 찾지 못했습니다.`);
}

function sendProgress(step) {
  try { chrome.runtime.sendMessage({ type: 'progress', step }); } catch (e) {}
}

chrome.runtime.onMessage.addListener((msg, sender, respond) => {

  // background.js로부터 수집 시작 명령 → 버튼 클릭 자동화
  if (msg.type === 'start_collect') {
    respond({ ok: true });

    (async () => {
      try {
        // DOM attribute로 MAIN world와 serverUrl 공유
        document.documentElement.setAttribute('data-minbest-server', msg.serverUrl);

        sendProgress('리뷰 페이지 준비 중...');
        await sleep(2000);

        sendProgress('검색 조건 초기화 중...');
        await clickByText('초기화');
        await sleep(1500);

        // 마지막 수집 날짜 기준으로 최소 기간 선택
        let periodBtn = '1년';
        let periodMsg = '전체 1년치 다운로드 중...';
        try {
          const r = await fetch(`${msg.serverUrl}/api/latest-review-date`);
          const d = await r.json();
          if (d.date) {
            const daysSince = Math.floor((Date.now() - new Date(d.date)) / 86400000);
            if (daysSince <= 7)        { periodBtn = '1주일'; periodMsg = `최근 ${daysSince}일치만 다운로드 중... (마지막 수집: ${d.date})`; }
            else if (daysSince <= 30)  { periodBtn = '1개월'; periodMsg = `최근 ${daysSince}일치만 다운로드 중... (마지막 수집: ${d.date})`; }
            else if (daysSince <= 90)  { periodBtn = '3개월'; periodMsg = `최근 ${daysSince}일치만 다운로드 중... (마지막 수집: ${d.date})`; }
            else if (daysSince <= 180) { periodBtn = '6개월'; periodMsg = `최근 ${daysSince}일치만 다운로드 중... (마지막 수집: ${d.date})`; }
          }
        } catch (e) {}

        sendProgress(periodMsg);
        await clickByText(periodBtn);
        await sleep(800);

        sendProgress('검색 실행 중...');
        await clickByText('검색');
        await sleep(6000);

        sendProgress('엑셀 다운로드 시작...');
        await clickByText('엑셀다운');
        await sleep(2500);

        // 모달 확인 버튼 클릭
        function findModalConfirmBtn() {
          return [...document.querySelectorAll('button')]
            .filter(b => b.offsetParent !== null)
            .find(b => {
              const t = b.textContent.trim();
              const inModal = b.closest('[role="dialog"], [class*="Modal"], [class*="modal"], [class*="Popup"], [class*="popup"]');
              return inModal && (t === '확인' || t === '다운로드' || t === '예');
            });
        }

        const confirmBtn = findModalConfirmBtn();
        if (confirmBtn) {
          sendProgress(`팝업 감지됨 — 확인 클릭 중... (${confirmBtn.textContent.trim()})`);
          confirmBtn.click();
          await sleep(2000);
          // 2차 팝업 대응
          const confirmBtn2 = findModalConfirmBtn();
          if (confirmBtn2) {
            sendProgress(`2차 팝업 감지됨 — 확인 클릭 중... (${confirmBtn2.textContent.trim()})`);
            confirmBtn2.click();
          }
        } else {
          sendProgress('다운로드 진행 중...');
        }
        // 이후는 background.js의 chrome.downloads.onCreated가 처리
      } catch (e) {
        sendProgress(`실패: ${e.message}`);
      }
    })();

    return true;
  }
});
