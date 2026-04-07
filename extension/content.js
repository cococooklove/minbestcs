let _excelResolve = null, _excelReject = null;

window.addEventListener('message', e => {
  if (e.source === window && e.data?.__minbest_type === 'excel' && _excelResolve) {
    _excelResolve(e.data.data);
    _excelResolve = null;
    _excelReject = null;
  }
});

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function waitForExcel(ms = 35000) {
  return new Promise((resolve, reject) => {
    _excelResolve = resolve;
    _excelReject = reject;
    setTimeout(() => {
      if (_excelReject) {
        _excelReject(new Error('Excel 다운로드 타임아웃 (35초). 팝업 구조 확인 필요.'));
        _excelResolve = null;
        _excelReject = null;
      }
    }, ms);
  });
}

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

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if (msg.type !== 'start_collect') return;
  (async () => {
    try {
      sendProgress('리뷰 페이지 준비 중...');
      await sleep(2000);

      sendProgress('검색 조건 초기화 중...');
      await clickByText('초기화');
      await sleep(1500);

      sendProgress('기간 1년 설정 중...');
      await clickByText('1년');
      await sleep(800);

      sendProgress('검색 실행 중...');
      await clickByText('검색');
      await sleep(6000);

      sendProgress('엑셀 다운로드 시작...');
      const excelPromise = waitForExcel(35000);

      await clickByText('엑셀다운');
      await sleep(2500);

      // 모달 안의 확인 버튼 클릭
      const allBtns = [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null);
      const confirmBtn = allBtns.find(b => {
        const t = b.textContent.trim();
        const inModal = b.closest('[role="dialog"], [class*="Modal"], [class*="modal"], [class*="Popup"], [class*="popup"]');
        return inModal && (t === '확인' || t === '다운로드' || t === '예');
      });
      if (confirmBtn) {
        sendProgress(`팝업 확인 클릭 (${confirmBtn.textContent.trim()})`);
        confirmBtn.click();
      } else {
        sendProgress('팝업 없음 — 다운로드 대기 중...');
      }

      sendProgress('파일 수신 중...');
      const data = await excelPromise;
      respond({ success: true, data });
    } catch (e) {
      respond({ success: false, error: e.message });
    }
  })();
  return true;
});

function sendProgress(step) {
  try { chrome.runtime.sendMessage({ type: 'progress', step }); } catch (e) {}
}
