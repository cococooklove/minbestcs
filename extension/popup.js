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

    // 셀러센터 탭이 열려 있고 로그인 페이지가 아닌지 확인
    const sellerTabs = await chrome.tabs.query({ url: 'https://sell.smartstore.naver.com/*' });
    const hasSellerTab = sellerTabs.some(t => t.url && !t.url.includes('login') && !t.url.includes('nidlogin'));

    // 셀러센터 도메인 쿠키가 하나라도 있으면 방문 이력 있음 (탭만 있고 쿠키 없으면 미로그인)
    const sellerCookies = await chrome.cookies.getAll({ domain: 'sell.smartstore.naver.com' });
    const hasSellerCookies = sellerCookies.length > 0;

    let loggedIn = false;
    if (!hasNaverLogin) {
      naverDot.className = 'dot red';
      naverStatus.innerHTML = '네이버 로그인 필요 &nbsp;<a href="https://nid.naver.com/nidlogin.login" target="_blank" style="color:#03c75a;font-weight:600;">로그인하기 →</a>';
    } else if (!hasSellerTab || !hasSellerCookies) {
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
    resultDiv.textContent = '수집 준비 중...';
    resultDiv.className = 'result';

    // 웹 UI 즉시 열기
    const collectUrl = serverUrl + '/?collecting=1';
    const existingTabs = await chrome.tabs.query({ url: serverUrl + '/*' });
    if (existingTabs.length > 0) {
      chrome.tabs.update(existingTabs[0].id, { active: true, url: collectUrl });
      chrome.windows.update(existingTabs[0].windowId, { focused: true });
    } else {
      chrome.tabs.create({ url: collectUrl });
    }

    try {
      // background service worker에 수집 위임
      await chrome.runtime.sendMessage({ type: 'collect', serverUrl });

      resultDiv.textContent = '수집 진행 중...';
      resultDiv.className = 'result';
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
