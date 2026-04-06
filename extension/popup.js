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

  // 일반 네이버 쿠키 (포털 로그인만 해도 존재하는 것들)
  const NAVER_BASE_COOKIES = new Set(['NID_AUT','NID_SES','NID_JKL','nid_inf','nid_slevel','nid_enctp','page_uid']);

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

    const hasNaverLogin = allCookies.some(c => c.name === 'NID_AUT' || c.name === 'NID_SES');
    // 셀러센터 전용 세션 쿠키 = 전체 쿠키에서 일반 네이버 쿠키를 뺀 것
    const hasSellerSession = allCookies.some(c => !NAVER_BASE_COOKIES.has(c.name));

    let loggedIn = false;
    if (!hasNaverLogin) {
      naverDot.className = 'dot red';
      naverStatus.innerHTML = '네이버 로그인 필요 &nbsp;<a href="https://nid.naver.com/nidlogin.login" target="_blank" style="color:#03c75a;font-weight:600;">로그인하기 →</a>';
    } else if (!hasSellerSession) {
      naverDot.className = 'dot red';
      naverStatus.innerHTML = '셀러센터 로그인 필요 &nbsp;<a href="https://sell.smartstore.naver.com/" target="_blank" style="color:#03c75a;font-weight:600;">셀러센터 열기 →</a>';
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

      const res = await fetch(`${serverUrl}/api/cookies`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookies: allCookies })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `서버 오류 (${res.status})`);

      resultDiv.textContent = '✓ 수집 시작! 웹에서 확인하세요.';
      resultDiv.className = 'result success';

      // 웹 UI 열기 (이미 열려있으면 포커스)
      const tabs = await chrome.tabs.query({ url: serverUrl + '/*' });
      if (tabs.length > 0) {
        chrome.tabs.update(tabs[0].id, { active: true });
        chrome.windows.update(tabs[0].windowId, { focused: true });
      } else {
        chrome.tabs.create({ url: serverUrl });
      }

    } catch (e) {
      resultDiv.textContent = '오류: ' + e.message;
      resultDiv.className = 'result error';
      collectBtn.disabled = false;
    }
  });
});
