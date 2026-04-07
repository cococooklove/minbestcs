chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url?.includes('naver.com')) {
    chrome.action.setBadgeText({ text: '●', tabId });
    chrome.action.setBadgeBackgroundColor({ color: '#03c75a', tabId });
  }
});

// Naver 다운로드 감지 — JS 몽키패칭 대신 브라우저 레벨에서 인터셉트
chrome.downloads.onCreated.addListener(async (item) => {
  if (!_serverUrl) return; // 수집 진행 중일 때만
  const url = item.url || '';
  const filename = item.filename || '';
  if (!url.includes('naver.com') && !url.includes('smartstore')) return;
  if (!url.match(/excel|xlsx|download|export/i) && !filename.match(/\.xlsx?$/i)) return;

  // Downloads 폴더 저장 취소
  chrome.downloads.cancel(item.id, () => {});

  // 셀러센터 탭에 fetch_and_upload 전달
  const tabs = await chrome.tabs.query({});
  const naverTab = tabs.find(t => t.url?.includes('sell.smartstore.naver.com'));
  if (!naverTab) {
    reportProgress(_serverUrl, '실패: 셀러센터 탭을 찾을 수 없습니다.');
    return;
  }

  chrome.tabs.sendMessage(naverTab.id, {
    type: 'fetch_and_upload',
    url,
    serverUrl: _serverUrl
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
