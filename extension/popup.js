const NAVER_DOMAINS = ['.naver.com', 'naver.com', 'sell.smartstore.naver.com'];

document.addEventListener('DOMContentLoaded', async () => {
  const urlInput   = document.getElementById('server-url');
  const saveBtn    = document.getElementById('save-url');
  const collectBtn = document.getElementById('collect-btn');
  const naverDot   = document.getElementById('naver-dot');
  const naverStatus = document.getElementById('naver-status');
  const resultDiv  = document.getElementById('result');

  // 서버 URL 로드 (config.js 우선, 없으면 저장된 값)
  const stored = await chrome.storage.sync.get(['serverUrl']);
  const defaultUrl = (typeof SERVER_URL !== 'undefined' && SERVER_URL) ? SERVER_URL : '';
  urlInput.value = stored.serverUrl || defaultUrl;

  saveBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim().replace(/\/$/, '');
    if (!url) return;
    await chrome.storage.sync.set({ serverUrl: url });
    resultDiv.textContent = '저장됨';
    resultDiv.className = 'result success';
    checkAll();
  });

  async function checkNaverLogin() {
    const seen = new Set();
    const allCookies = [];
    for (const domain of NAVER_DOMAINS) {
      const cookies = await chrome.cookies.getAll({ domain });
      for (const c of cookies) {
        const key = c.name + '|' + c.domain;
        if (!seen.has(key)) { seen.add(key); allCookies.push(c); }
      }
    }

    // 네이버 로그인 여부 (NID_AUT = 실제 로그인 토큰)
    const hasNaverLogin = allCookies.some(c => c.name === 'NID_AUT');

    // 셀러센터 세션 쿠키 확인 (탭 오픈 여부가 아닌 실제 세션 쿠키로 판단)
    const sellerCookies = await chrome.cookies.getAll({ domain: 'sell.smartstore.naver.com' });
    const hasSellerSession = sellerCookies.some(c =>
      ['SID', 'JSESSIONID', 'nstore_prd_session', 'NSP_SESSION'].includes(c.name)
    );

    let loggedIn = false;
    if (!hasNaverLogin) {
      naverDot.className = 'dot red';
      naverStatus.innerHTML = '네이버 로그인 필요 &nbsp;<a href="https://nid.naver.com/nidlogin.login" target="_blank" style="color:#03c75a;font-weight:600;">로그인하기 →</a>';
    } else if (!hasSellerSession) {
      naverDot.className = 'dot red';
      naverStatus.innerHTML = '셀러센터를 열고 로그인해주세요 &nbsp;<a href="https://sell.smartstore.naver.com/" target="_blank" style="color:#03c75a;font-weight:600;">열기 →</a>';
    } else {
      naverDot.className = 'dot green';
      naverStatus.textContent = '셀러센터 로그인됨 ✓';
      loggedIn = true;
    }
    return { loggedIn, allCookies };
  }

  async function checkAll() {
    const { loggedIn } = await checkNaverLogin();
    const hasUrl = !!urlInput.value.trim();
    collectBtn.disabled = !loggedIn || !hasUrl;
  }

  await checkAll();

  collectBtn.addEventListener('click', async () => {
    const serverUrl = urlInput.value.trim().replace(/\/$/, '');
    if (!serverUrl) {
      resultDiv.textContent = '서버 URL을 입력해주세요.';
      resultDiv.className = 'result error';
      return;
    }

    collectBtn.disabled = true;
    resultDiv.textContent = '쿠키 전송 중...';
    resultDiv.className = 'result';

    try {
      const seen = new Set();
      const allCookies = [];
      for (const domain of NAVER_DOMAINS) {
        const cookies = await chrome.cookies.getAll({ domain });
        for (const c of cookies) {
          const key = c.name + '|' + c.domain;
          if (!seen.has(key)) { seen.add(key); allCookies.push(c); }
        }
      }

      if (allCookies.length === 0) {
        throw new Error('쿠키를 찾을 수 없습니다. 네이버에 로그인해주세요.');
      }

      let res = await fetch(`${serverUrl}/api/cookies`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookies: allCookies })
      });

      // 이전 수집이 비정상 종료돼 플래그가 고착된 경우 자동 리셋 후 재시도
      if (res.status === 400) {
        const errData = await res.json();
        if (errData.error && errData.error.includes('수집 중')) {
          await fetch(`${serverUrl}/api/reset`, { method: 'POST' });
          res = await fetch(`${serverUrl}/api/cookies`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cookies: allCookies })
          });
        }
      }

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `서버 오류 (${res.status})`);

      resultDiv.textContent = '수집 중... 잠시 기다려주세요.';
      resultDiv.className = 'result';

      // 웹 UI 열기 (이미 열려있으면 포커스)
      const tabs = await chrome.tabs.query({ url: serverUrl + '/*' });
      if (tabs.length > 0) {
        chrome.tabs.update(tabs[0].id, { active: true });
        chrome.windows.update(tabs[0].windowId, { focused: true });
      } else {
        chrome.tabs.create({ url: serverUrl });
      }

      // 팝업에서 수집 완료까지 상태 폴링
      await pollStatus(serverUrl);

    } catch (e) {
      resultDiv.textContent = '오류: ' + e.message;
      resultDiv.className = 'result error';
      collectBtn.disabled = false;
    }
  });

  async function pollStatus(serverUrl) {
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${serverUrl}/api/status`);
        const data = await res.json();
        if (data.scraping) {
          resultDiv.textContent = data.step || '수집 중...';
          resultDiv.className = 'result';
        } else {
          clearInterval(interval);
          if (data.step && data.step.startsWith('실패')) {
            resultDiv.textContent = '❌ ' + data.step;
            resultDiv.className = 'result error';
          } else {
            resultDiv.textContent = '✓ 수집 완료! 웹에서 결과를 확인하세요.';
            resultDiv.className = 'result success';
          }
          collectBtn.disabled = false;
        }
      } catch {
        clearInterval(interval);
        resultDiv.textContent = '서버 연결 오류';
        resultDiv.className = 'result error';
        collectBtn.disabled = false;
      }
    }, 1500);
  }
});
