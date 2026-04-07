chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url?.includes('naver.com')) {
    chrome.action.setBadgeText({ text: '●', tabId });
    chrome.action.setBadgeBackgroundColor({ color: '#03c75a', tabId });
  }
});

// Naver 다운로드 감지 — 쿠키 직접 추출 후 background에서 fetch
chrome.downloads.onCreated.addListener(async (item) => {
  if (!_serverUrl) return;
  const url = item.url || '';
  const filename = item.filename || '';
  if (!url.includes('naver.com') && !url.includes('smartstore')) return;
  if (!url.match(/excel|xlsx|download|export/i) && !filename.match(/\.xlsx?$/i)) return;

  const sv = _serverUrl;
  reportProgress(sv, '파일 수신 중...');

  try {
    // 모든 naver.com 관련 도메인 쿠키 수집
    const [cookies1, cookies2, cookies3] = await Promise.all([
      chrome.cookies.getAll({ domain: 'naver.com' }),
      chrome.cookies.getAll({ domain: 'sell.smartstore.naver.com' }),
      chrome.cookies.getAll({ domain: '.naver.com' }),
    ]);

    const allCookies = [...cookies1, ...cookies2, ...cookies3];
    // 중복 제거
    const seen = new Set();
    const uniqueCookies = allCookies.filter(c => {
      const key = `${c.domain}|${c.name}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    const cookieStr = uniqueCookies.map(c => `${c.name}=${c.value}`).join('; ');

    // background service worker에서 fetch — Cookie 헤더 직접 설정 가능
    const res = await fetch(url, {
      method: 'GET',
      headers: {
        'Cookie': cookieStr,
        'Referer': 'https://sell.smartstore.naver.com/',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*',
      }
    });

    if (!res.ok) {
      // 실패 시 로컬 다운로드 파일을 직접 읽는 방식으로 폴백
      reportProgress(sv, `fetch 실패 (${res.status}) — 로컬 파일 감지 대기 중...`);
      _pendingDownloadId = item.id;
      return;
    }

    // 다운로드 취소 (Downloads 폴더에 중복 저장 방지)
    chrome.downloads.cancel(item.id, () => {});

    const buf = await res.arrayBuffer();
    reportProgress(sv, '서버로 파일 전송 중...');

    const blob = new Blob([buf], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    });
    const form = new FormData();
    form.append('file', blob, 'reviews.xlsx');
    const r = await fetch(`${sv}/api/upload-excel`, { method: 'POST', body: form });
    const data = await r.json();
    reportProgress(sv, r.ok ? '완료' : `실패: ${data.error || '업로드 실패'}`);
  } catch (e) {
    reportProgress(sv, `실패: ${e.message}`);
  }
});

// 로컬 다운로드 완료 감지 — fetch 실패 폴백
let _pendingDownloadId = null;
chrome.downloads.onChanged.addListener(async (delta) => {
  if (!_serverUrl || !_pendingDownloadId) return;
  if (delta.id !== _pendingDownloadId) return;
  if (delta.state?.current !== 'complete') return;

  _pendingDownloadId = null;
  const sv = _serverUrl;

  try {
    const [downloadItem] = await chrome.downloads.search({ id: delta.id });
    if (!downloadItem?.filename) {
      reportProgress(sv, '실패: 다운로드 파일 경로 없음');
      return;
    }

    reportProgress(sv, '다운로드 완료 — 파일 읽는 중...');

    // background에서 file:// 프로토콜로 직접 읽기
    const fileUrl = 'file://' + downloadItem.filename.replace(/\\/g, '/');
    const fileRes = await fetch(fileUrl);
    if (!fileRes.ok) {
      reportProgress(sv, '실패: 로컬 파일 읽기 실패 (파일 접근 권한 확인 필요)');
      return;
    }

    reportProgress(sv, '파일 읽기 완료 — 서버로 전송 중...');
    const buf = await fileRes.arrayBuffer();
    const blob = new Blob([buf], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    });
    const form = new FormData();
    form.append('file', blob, 'reviews.xlsx');

    const r = await fetch(`${sv}/api/upload-excel`, { method: 'POST', body: form });
    const data = await r.json();

    if (r.ok) {
      // 업로드 성공 후 로컬 파일 삭제
      chrome.downloads.removeFile(delta.id, () => {});
      reportProgress(sv, '완료');
    } else {
      reportProgress(sv, `실패: ${data.error || '업로드 실패'}`);
    }
  } catch (e) {
    reportProgress(sv, `실패: ${e.message}`);
  }
});

let _serverUrl = '';

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if (msg.type === 'collect') {
    _serverUrl = msg.serverUrl;
    handleCollect(msg.serverUrl).catch(async e => {
      await reportProgress(msg.serverUrl, `실패: ${e.message}`);
    });
    respond({ ok: true });
  } else if (msg.type === 'progress') {
    if (_serverUrl) reportProgress(_serverUrl, msg.step);
  }
});

async function reportProgress(serverUrl, step) {
  try {
    await fetch(`${serverUrl}/api/client-progress`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step })
    });
  } catch (e) {}
}

async function waitForTabLoad(tabId) {
  return new Promise(resolve => {
    chrome.tabs.get(tabId, tab => {
      if (tab?.status === 'complete') { return setTimeout(resolve, 1500); }
      function listener(id, info) {
        if (id === tabId && info.status === 'complete') {
          chrome.tabs.onUpdated.removeListener(listener);
          setTimeout(resolve, 1500);
        }
      }
      chrome.tabs.onUpdated.addListener(listener);
    });
  });
}

async function handleCollect(serverUrl) {
  const reviewUrl = 'https://sell.smartstore.naver.com/#/review/search';
  await reportProgress(serverUrl, '셀러센터 리뷰 페이지 열기...');

  const existing = await chrome.tabs.query({ url: 'https://sell.smartstore.naver.com/*' });
  let tab;
  if (existing.length > 0) {
    tab = existing[0];
    await chrome.tabs.update(tab.id, { active: true, url: reviewUrl });
    await chrome.windows.update(tab.windowId, { focused: true });
  } else {
    tab = await chrome.tabs.create({ url: reviewUrl });
  }

  await waitForTabLoad(tab.id);

  chrome.tabs.sendMessage(tab.id, { type: 'start_collect', serverUrl });
}
