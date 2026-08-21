// IMPORTANT: replace this with your real deployed backend URL once hosted
// e.g. "https://your-app-name.onrender.com"
const BACKEND_URL = "https://YOUR-BACKEND-DOMAIN.example.com";

const btn = document.getElementById("downloadBtn");
const status = document.getElementById("status");

function setStatus(msg, cls) {
  status.textContent = msg;
  status.className = cls || "";
}

// Try to prefill the URL box with the current tab's link if it looks
// like a video page (nice-to-have convenience, not required).
chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const tabUrl = tabs[0]?.url;
  if (tabUrl && /youtube\.com|youtu\.be|facebook\.com|instagram\.com|tiktok\.com/.test(tabUrl)) {
    document.getElementById("url").value = tabUrl;
  }
});

btn.addEventListener("click", () => {
  const url = document.getElementById("url").value.trim();
  const type = document.getElementById("type").value;
  const quality = document.getElementById("quality").value;

  if (!url) {
    setStatus("Please paste a video link first.", "error");
    return;
  }

  btn.disabled = true;
  setStatus("Starting download...", "");

  const apiUrl =
    `${BACKEND_URL}/download?url=${encodeURIComponent(url)}` +
    `&type=${encodeURIComponent(type)}&quality=${encodeURIComponent(quality)}`;

  // chrome.downloads.download() makes the browser GET this URL directly
  // and save the response as a file - no need to fetch/blob it ourselves.
  chrome.downloads.download({ url: apiUrl }, (downloadId) => {
    btn.disabled = false;
    if (chrome.runtime.lastError || !downloadId) {
      setStatus("Error: " + (chrome.runtime.lastError?.message || "Download failed."), "error");
    } else {
      setStatus("Download started!", "success");
    }
  });
});
