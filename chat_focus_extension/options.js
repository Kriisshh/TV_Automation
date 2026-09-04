const el = document.getElementById("min");
const status = document.getElementById("status");

chrome.storage.local.get({ checkMinutes: 2 }, (r) => {
  el.value = r.checkMinutes;
});

document.getElementById("save").addEventListener("click", () => {
  let v = parseFloat(el.value);
  if (!v || v <= 0) v = 2;
  el.value = v;
  chrome.storage.local.set({ checkMinutes: v }, () => {
    status.textContent = "Saved";
    setTimeout(() => (status.textContent = ""), 1500);
  });
});
