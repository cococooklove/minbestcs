chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url?.includes('naver.com')) {
    chrome.action.setBadgeText({ text: '●', tabId });
    chrome.action.setBadgeBackgroundColor({ color: '#03c75a', tabId });
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

  const result = await chrome.tabs.sendMessage(tab.id, { type: 'start_collect' });

  if (!result?.success) {
    throw new Error(result?.error || '수집 실패');
  }

  await reportProgress(serverUrl, '서버로 파일 전송 중...');
  const bytes = new Uint8Array(result.data);
  const blob = new Blob([bytes], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  });
  const formData = new FormData();
  formData.append('file', blob, 'reviews.xlsx');

  const res = await fetch(`${serverUrl}/api/upload-excel`, {
    method: 'POST',
    body: formData
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '서버 업로드 실패');

  await reportProgress(serverUrl, '완료');
}
