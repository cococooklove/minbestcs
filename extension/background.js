chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url?.includes('naver.com')) {
    chrome.action.setBadgeText({ text: '●', tabId });
    chrome.action.setBadgeBackgroundColor({ color: '#03c75a', tabId });
  }
});

// Naver 다운로드 감지 — 브라우저 레벨 인터셉트 후 MAIN world에서 재요청
chrome.downloads.onCreated.addListener(async (item) => {
  if (!_serverUrl) return;
  const url = item.url || '';
  const filename = item.filename || '';
  if (!url.includes('naver.com') && !url.includes('smartstore')) return;
  if (!url.match(/excel|xlsx|download|export/i) && !filename.match(/\.xlsx?$/i)) return;

  // Downloads 폴더 저장 취소
  chrome.downloads.cancel(item.id, () => {});

  const tabs = await chrome.tabs.query({});
  const naverTab = tabs.find(t => t.url?.includes('sell.smartstore.naver.com'));
  if (!naverTab) {
    reportProgress(_serverUrl, '실패: 셀러센터 탭을 찾을 수 없습니다.');
    return;
  }

  const sv = _serverUrl;
  reportProgress(sv, '파일 수신 중...');

  // MAIN world에서 실행 → sell.smartstore.naver.com origin으로 fetch (CORS 없음)
  chrome.scripting.executeScript({
    target: { tabId: naverTab.id },
    world: 'MAIN',
    func: async (downloadUrl, serverUrl) => {
      try {
        const res = await fetch(downloadUrl, { credentials: 'include' });
        if (!res.ok) {
          document.dispatchEvent(new CustomEvent('__minbest_progress', { detail: `실패: 파일 요청 실패 (${res.status})` }));
          return;
        }
        const buf = await res.arrayBuffer();
        const blob = new Blob([buf], {
          type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        });
        document.dispatchEvent(new CustomEvent('__minbest_progress', { detail: '서버로 파일 전송 중...' }));
        const form = new FormData();
        form.append('file', blob, 'reviews.xlsx');
        const r = await fetch(`${serverUrl}/api/upload-excel`, { method: 'POST', body: form });
        const data = await r.json();
        document.dispatchEvent(new CustomEvent('__minbest_progress', {
          detail: r.ok ? '완료' : `실패: ${data.error || '업로드 실패'}`
        }));
      } catch (e) {
        document.dispatchEvent(new CustomEvent('__minbest_progress', { detail: `실패: ${e.message}` }));
      }
    },
    args: [url, sv]
  });
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

  // serverUrl을 content script에 전달, 이후 업로드는 content script가 직접 처리
  chrome.tabs.sendMessage(tab.id, { type: 'start_collect', serverUrl });
}
